import asyncio
import json
import os
import re
import time
import uuid
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urlencode

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, Reply, File
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


# ==================== 独立文件存储的记忆管理类 ====================
class MemoryManager:
    def __init__(self, data_dir: str, max_memories_per_user: int = 100):
        self.data_dir = data_dir
        self.max_memories_per_user = max_memories_per_user
        self._lock = asyncio.Lock()
        self._file_path = os.path.join(data_dir, "memories.json")
        self._ensure_file()

    def _ensure_file(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self._file_path):
            self._save_data({"memories": []})

    def _load_data(self) -> dict:
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"memories": []}

    def _save_data(self, data: dict):
        with open(self._file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _cleanup_if_needed(self, user_id: str):
        if self.max_memories_per_user <= 0:
            return
        data = self._load_data()
        memories = data.get("memories", [])
        user_memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if len(user_memories) > self.max_memories_per_user:
            user_memories.sort(key=lambda x: x.get("updated_at", ""))
            to_delete_ids = [m["id"] for m in user_memories[:len(user_memories) - self.max_memories_per_user]]
            memories = [m for m in memories if m.get("id") not in to_delete_ids]
            self._save_data({"memories": memories})
            logger.info(f"[MemoryManager] 已清理用户 {user_id} 的 {len(to_delete_ids)} 条旧记忆")

    async def add_memory(self, user_id: str, content: str, tags: list = None, importance: int = 5) -> str:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            memory_id = str(uuid.uuid4())[:8]
            new_memory = {
                "id": memory_id,
                "user_id": str(user_id),
                "content": content,
                "tags": tags or [],
                "importance": max(1, min(10, importance)),
                "created_at": self._get_timestamp(),
                "updated_at": self._get_timestamp()
            }
            memories.append(new_memory)
            self._save_data({"memories": memories})
            await self._cleanup_if_needed(user_id)
            logger.info(f"[MemoryManager] 添加记忆成功: {memory_id} 用户: {user_id}")
            return memory_id

    async def update_memory(self, memory_id: str, content: str = None, tags: list = None, importance: int = None) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            for memory in memories:
                if memory.get("id") == memory_id:
                    if content is not None:
                        memory["content"] = content
                    if tags is not None:
                        memory["tags"] = tags
                    if importance is not None:
                        memory["importance"] = max(1, min(10, importance))
                    memory["updated_at"] = self._get_timestamp()
                    self._save_data({"memories": memories})
                    return True
            return False

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            original_len = len(memories)
            memories = [m for m in memories if m.get("id") != memory_id]
            if len(memories) < original_len:
                self._save_data({"memories": memories})
                return True
            return False

    async def get_memories(self, user_id: str = None, keyword: str = None,
                          limit: int = 10, sort_by: str = "updated_at") -> List[dict]:
        data = self._load_data()
        memories = data.get("memories", [])
        if user_id:
            memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if keyword:
            keyword_lower = keyword.lower()
            filtered = []
            for m in memories:
                if keyword_lower in m.get("content", "").lower() or any(keyword_lower in tag.lower() for tag in m.get("tags", [])):
                    filtered.append(m)
            memories = filtered
        if sort_by == "importance":
            memories.sort(key=lambda x: x.get("importance", 0), reverse=True)
        elif sort_by in ["updated_at", "created_at"]:
            memories.sort(key=lambda x: x.get(sort_by, ""), reverse=True)
        return memories[:limit]

    async def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        memories = await self.get_memories(limit=10000)
        for m in memories:
            if m.get("id") == memory_id:
                return m
        return None

    async def get_latest_memories_for_inject(self, user_id: str, count: int = 5) -> List[dict]:
        return await self.get_memories(user_id=user_id, limit=count, sort_by="updated_at")


# ==================== 数据库管理（定时任务） ====================
class DatabaseManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "commands_db.json")
        self.status_path = os.path.join(data_dir, "status.json")
        self._lock = asyncio.Lock()
        self._init_storage()

    def _init_storage(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.db_path):
            self._save_json(self.db_path, {"scheduled_commands": [], "version": "1.0"})
        if not os.path.exists(self.status_path):
            self._save_json(self.status_path, {"current_status": "online", "status_name": "在线"})

    def _load_json(self, filepath: str, default: Any = None) -> Any:
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[DatabaseManager] 读取文件失败: {e}")
        return default

    def _save_json(self, filepath: str, data: Any) -> bool:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath + ".tmp", 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(filepath + ".tmp", filepath)
            return True
        except Exception as e:
            logger.error(f"[DatabaseManager] 保存文件失败: {e}")
            return False

    async def save_scheduled_command(self, task_id: str, command_type: str,
                                     params: dict, execute_time: datetime,
                                     recurrence: str = "once",
                                     session_info: dict = None) -> bool:
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            record = {
                "id": task_id,
                "command_type": command_type,
                "params": json.dumps(params, ensure_ascii=False),
                "execute_time": execute_time.isoformat(),
                "created_at": datetime.now().isoformat(),
                "executed": 0,
                "recurrence": recurrence,
                "session_info": session_info
            }
            for i, cmd in enumerate(commands):
                if isinstance(cmd, dict) and cmd.get('id') == task_id:
                    commands[i] = record
                    break
            else:
                commands.append(record)
            db_data["scheduled_commands"] = commands
            return self._save_json(self.db_path, db_data)

    async def get_pending_commands(self) -> List[dict]:
        db_data = self._load_json(self.db_path, {"scheduled_commands": []})
        commands = db_data.get("scheduled_commands", [])
        return [cmd for cmd in commands if isinstance(cmd, dict) and cmd.get("executed") == 0]

    async def get_all_commands(self, include_executed: bool = False) -> List[dict]:
        db_data = self._load_json(self.db_path, {"scheduled_commands": []})
        commands = db_data.get("scheduled_commands", [])
        if include_executed:
            return commands
        return await self.get_pending_commands()

    async def mark_command_executed(self, task_id: str, executed: int = 1):
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            for cmd in commands:
                if isinstance(cmd, dict) and cmd.get('id') == task_id:
                    cmd['executed'] = executed
                    cmd['completed_at'] = datetime.now().isoformat()
                    break
            db_data["scheduled_commands"] = commands
            self._save_json(self.db_path, db_data)

    async def delete_command(self, task_id: str):
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            commands = [cmd for cmd in commands if isinstance(cmd, dict) and cmd.get('id') != task_id]
            db_data["scheduled_commands"] = commands
            self._save_json(self.db_path, db_data)

    async def cancel_command(self, task_id: str):
        await self.mark_command_executed(task_id, 2)

    async def save_status(self, status_key: str, status_name: str, end_time: Optional[datetime]):
        async with self._lock:
            record = {
                "current_status": status_key,
                "status_name": status_name,
                "end_time": end_time.isoformat() if end_time else "",
                "updated_at": datetime.now().isoformat()
            }
            self._save_json(self.status_path, record)

    async def load_status(self) -> Optional[dict]:
        return self._load_json(self.status_path)

    async def clear_status(self):
        await self.save_status("online", "在线", None)


# ==================== QQ空间相关 ====================
class QzoneSession:
    def __init__(self):
        self.uin: str = ""
        self.cookie: str = ""
        self.gtk: str = ""
        self.client = None
        self.initialized = False

    def _cookie_to_dict(self, cookie_str: str) -> dict:
        cookie_dict = {}
        for item in cookie_str.split(';'):
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookie_dict[key] = value
        return cookie_dict

    def _calc_gtk(self, skey: str) -> str:
        hash_val = 5381
        for char in skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 0x7fffffff)

    async def initialize(self, client) -> bool:
        try:
            self.client = client
            login_info = await client.call_action('get_login_info')
            self.uin = str(login_info.get('user_id', ''))
            if not self.uin:
                return False
            try:
                creds = await client.call_action('get_credentials', domain='qzone.qq.com')
                self.cookie = creds.get('cookies', '')
            except Exception:
                try:
                    cookies = await client.call_action('get_cookies', domain='qzone.qq.com')
                    self.cookie = cookies.get('cookies', '')
                except:
                    return False
            if not self.cookie:
                return False
            cookie_dict = self._cookie_to_dict(self.cookie)
            key = cookie_dict.get('p_skey') or cookie_dict.get('skey')
            if not key:
                return False
            self.gtk = self._calc_gtk(key)
            self.initialized = True
            logger.info(f"[QzoneSession] 初始化成功")
            return True
        except Exception as e:
            logger.error(f"[QzoneSession] 初始化失败: {e}")
            return False

    async def ensure_initialized(self, client) -> bool:
        if self.initialized:
            return True
        return await self.initialize(client)


class QzoneAPI:
    def __init__(self, session: QzoneSession):
        self.session = session

    async def publish_post(self, text: str, images: list = None) -> dict:
        images = images or []
        if not self.session.initialized:
            return {"success": False, "msg": "会话未初始化"}
        try:
            url = f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={self.session.gtk}"
            payload = {
                'syn_tweet_verson': '1',
                'con': text,
                'feedversion': '1',
                'ver': '1',
                'ugc_right': '1',
                'to_sign': '0',
                'hostuin': self.session.uin,
                'code_version': '1',
                'format': 'fs',
                'qzreferrer': f'https://user.qzone.qq.com/{self.session.uin}/infocenter',
            }
            encoded_data = urlencode(payload)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Cookie': self.session.cookie,
                'Origin': 'https://user.qzone.qq.com',
                'Referer': f'https://user.qzone.qq.com/{self.session.uin}/infocenter',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, data=encoded_data, headers=headers, timeout=30) as resp:
                    response_text = await resp.text()
                    if '"code":0' in response_text or '"code": 0' in response_text:
                        return {"success": True, "msg": "发表成功"}
                    return {"success": False, "msg": f"响应: {response_text[:200]}"}
        except Exception as e:
            return {"success": False, "msg": str(e)}


class ScheduledTask:
    def __init__(self, task_id: str, target_id: str, message: str, send_time: datetime,
                 chat_type: str, target_name: str = ""):
        self.task_id = task_id
        self.target_id = target_id
        self.message = message
        self.send_time = send_time
        self.chat_type = chat_type
        self.target_name = target_name
        self.cancelled = False
        self.completed = False


class QQStatusManager:
    def __init__(self):
        self.current_status: Optional[str] = "online"
        self.current_status_name: str = "在线"
        self.status_end_time: Optional[datetime] = None
        self.restore_task: Optional[asyncio.Task] = None
        self.pending_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.db_manager: Optional[DatabaseManager] = None

    def set_db_manager(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def restore_from_db(self, client):
        if not self.db_manager:
            return
        try:
            record = await self.db_manager.load_status()
            if not record:
                return
            status_key = record.get('current_status', 'online')
            end_time_str = record.get('end_time', '')
            if status_key == 'online' or not end_time_str:
                return
            end_time = datetime.fromisoformat(end_time_str)
            now = datetime.now()
            if end_time <= now:
                await self._force_set_online(client)
            else:
                status_info = self.get_status_info(status_key)
                if status_info:
                    self.current_status = status_key
                    self.current_status_name = status_info['name']
                    self.status_end_time = end_time
                    remain_minutes = (end_time - now).total_seconds() / 60
                    self.restore_task = asyncio.create_task(
                        self._auto_restore_online(client, int(remain_minutes))
                    )
        except Exception as e:
            logger.error(f"[QQStatusManager] 恢复状态失败: {e}")

    async def _force_set_online(self, client):
        try:
            params = {"status": 10, "ext_status": 0, "battery_status": 0}
            await client.call_action('set_online_status', **params)
            self.current_status = "online"
            self.current_status_name = "在线"
            self.status_end_time = None
            if self.db_manager:
                await self.db_manager.clear_status()
        except Exception as e:
            logger.error(f"[QQStatusManager] 强制恢复在线失败: {e}")

    def get_status_info(self, status_key: str) -> Optional[dict]:
        BASIC_STATUS = {
            "online": {"name": "在线", "status": 10, "ext": 0},
            "qme": {"name": "Q我吧", "status": 60, "ext": 0},
            "away": {"name": "离开", "status": 30, "ext": 0},
            "busy": {"name": "忙碌", "status": 50, "ext": 0},
            "dnd": {"name": "请勿打扰", "status": 70, "ext": 0},
            "invisible": {"name": "隐身", "status": 40, "ext": 0},
        }
        FUN_STATUS = {
            "listening": {"name": "听歌中", "status": 10, "ext": 1028},
            "sleeping": {"name": "睡觉中", "status": 10, "ext": 1016},
            "studying": {"name": "学习中", "status": 10, "ext": 1018},
        }
        if status_key in BASIC_STATUS:
            return BASIC_STATUS[status_key]
        return FUN_STATUS.get(status_key)

    def get_current_status_desc(self) -> str:
        if self.current_status == "online":
            return "当前状态：在线"
        now = datetime.now()
        if self.status_end_time and self.status_end_time > now:
            remain = self.status_end_time - now
            remain_min = remain.seconds // 60 + remain.days * 1440
            return f"当前状态：{self.current_status_name}（还剩约{remain_min}分钟）"
        return f"当前状态：{self.current_status_name}"

    def is_status_active(self) -> bool:
        if self.current_status == "online":
            return False
        if self.status_end_time and datetime.now() < self.status_end_time:
            return True
        return False

    async def set_status(self, client, status_key: str, duration_minutes: int, delay_minutes: int = 0) -> dict:
        async with self._lock:
            status_info = self.get_status_info(status_key)
            if not status_info:
                return {"success": False, "msg": f"无效的状态码: {status_key}"}
            if delay_minutes <= 0:
                return await self._execute_set_status(client, status_key, duration_minutes)
            else:
                async def delayed_task():
                    await asyncio.sleep(delay_minutes * 60)
                    await self._execute_set_status(client, status_key, duration_minutes)
                self.pending_task = asyncio.create_task(delayed_task())
                return {"success": True, "msg": f"已设置定时状态：{delay_minutes}分钟后切换", "is_pending": True}

    async def _execute_set_status(self, client, status_key: str, duration_minutes: int) -> dict:
        status_info = self.get_status_info(status_key)
        try:
            params = {"status": status_info["status"], "ext_status": status_info["ext"], "battery_status": 0}
            await client.call_action('set_online_status', **params)
            if status_key == "online":
                if self.restore_task and not self.restore_task.done():
                    self.restore_task.cancel()
                self.current_status = "online"
                self.current_status_name = "在线"
                self.status_end_time = None
                if self.db_manager:
                    await self.db_manager.clear_status()
                return {"success": True, "msg": "状态已恢复为「在线」", "is_online": True}
            self.current_status = status_key
            self.current_status_name = status_info['name']
            self.status_end_time = datetime.now() + timedelta(minutes=duration_minutes)
            if self.db_manager:
                await self.db_manager.save_status(status_key, status_info['name'], self.status_end_time)
            self.restore_task = asyncio.create_task(self._auto_restore_online(client, duration_minutes))
            return {
                "success": True,
                "msg": f"状态已设置为「{status_info['name']}」，持续{duration_minutes}分钟",
                "end_time": self.status_end_time.strftime("%H:%M:%S")
            }
        except Exception as e:
            return {"success": False, "msg": f"设置失败: {str(e)}"}

    async def _auto_restore_online(self, client, delay_minutes: int):
        try:
            await asyncio.sleep(delay_minutes * 60)
            if not client:
                return
            params = {"status": 10, "ext_status": 0, "battery_status": 0}
            await client.call_action('set_online_status', **params)
            async with self._lock:
                self.current_status = "online"
                self.current_status_name = "在线"
                self.status_end_time = None
                self.restore_task = None
                if self.db_manager:
                    await self.db_manager.clear_status()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[QQStatusManager] 自动恢复在线失败: {e}")


class ScheduledCommandExecutor:
    def __init__(self, plugin: 'Main'):
        self.plugin = plugin
        self.db_manager = plugin.db_manager
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self._stop_check = False
        self._lock = asyncio.Lock()

    async def start_periodic_check(self):
        self._stop_check = False
        while not self._stop_check:
            await asyncio.sleep(60)
            if not self._stop_check:
                try:
                    await self._check_and_execute_pending()
                except Exception as e:
                    logger.error(f"[ScheduledCommandExecutor] 定时检查失败: {e}")

    def stop_periodic_check(self):
        self._stop_check = True

    async def schedule_command(self, task_id: str, execute_time: datetime, command_type: str, params: dict, session_info: dict = None):
        pass

    def cancel_task(self, task_id: str):
        if task_id in self.running_tasks:
            task = self.running_tasks[task_id]
            if not task.done():
                task.cancel()
            del self.running_tasks[task_id]

    async def _check_and_execute_pending(self):
        pending = await self.db_manager.get_pending_commands()
        now = datetime.now()
        for cmd in pending:
            task_id = cmd.get('id')
            if task_id in self.running_tasks:
                continue
            try:
                execute_time_str = cmd.get('execute_time', '')
                if not execute_time_str:
                    continue
                execute_time = datetime.fromisoformat(execute_time_str)
                if (now - execute_time).total_seconds() >= 0:
                    await self.db_manager.mark_command_executed(task_id, 2)
                    task = asyncio.create_task(
                        self._execute_command(cmd['id'], cmd['command_type'], json.loads(cmd['params']), cmd.get('session_info'))
                    )
                    self.running_tasks[task_id] = task
            except Exception as e:
                logger.error(f"[ScheduledCommandExecutor] 处理指令失败: {e}")

    async def _execute_command(self, task_id: str, command_type: str, params: dict, session_info: dict = None):
        try:
            client = self.plugin._client
            if not client:
                return
            if command_type == "qzone_post":
                content = params.get("content", "")
                if content:
                    success = await self.plugin.session.initialize(client)
                    if success:
                        await self.plugin.qzone.publish_post(content)
            elif command_type == "status_change":
                status = params.get("status", "online")
                duration = params.get("duration_minutes", 30)
                await self.plugin.status_manager.set_status(client, status, duration, 0)
            elif command_type == "send_message":
                target_id = params.get("target_id", "")
                message = params.get("message", "")
                chat_type = params.get("chat_type", "group")
                if target_id and message:
                    if chat_type == "group":
                        await client.call_action('send_group_msg', group_id=int(target_id), message=message)
                    else:
                        await client.call_action('send_private_msg', user_id=int(target_id), message=message)
            elif command_type == "llm_remind":
                prompt = params.get("prompt", "")
                if prompt and session_info:
                    unified_msg_origin = session_info.get('unified_msg_origin')
                    if unified_msg_origin:
                        remind_message = f"[定时提醒 #{task_id}]\n{prompt}"
                        await self.plugin.context.send_message(unified_msg_origin, MessageChain().message(remind_message))
            await self.db_manager.mark_command_executed(task_id, 1)
        except Exception as e:
            logger.error(f"[ScheduledCommandExecutor] 执行任务失败: {e}")
            await self.db_manager.mark_command_executed(task_id, -1)
        finally:
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]


class EmailSender:
    def __init__(self, config: AstrBotConfig):
        self.config = config

    def _get_smtp_settings(self) -> Tuple[str, int, str, str]:
        sender = self.config.get("email_sender", "").strip()
        auth_code = self.config.get("email_authorization_code", "").strip()
        server = self.config.get("email_smtp_server", "smtp.qq.com").strip()
        port = self.config.get("email_smtp_port", 465)
        return server, port, sender, auth_code

    async def send_email(self, to_email: str, subject: str, content: str, sender_nickname: str = "") -> dict:
        server, port, sender, auth_code = self._get_smtp_settings()
        logger.info(f"[EmailSender] 配置: sender={sender}, auth_code={'已配置' if auth_code else '未配置'}, server={server}, port={port}")

        if not sender or not auth_code:
            return {"success": False, "msg": "❌ 发件人邮箱或授权码未配置，请在插件配置中填写 email_sender 和 email_authorization_code 后重载插件"}

        to_email = to_email.strip()
        if not to_email:
            return {"success": False, "msg": "收件人邮箱不能为空"}

        try:
            msg = MIMEText(content, "plain", "utf-8")
            from_addr = formataddr((sender_nickname, sender), charset="utf-8")
            msg["From"] = from_addr
            msg["To"] = formataddr(("", to_email), charset="utf-8")
            msg["Subject"] = Header(subject or "来自AstrBot的邮件", "utf-8")

            loop = asyncio.get_running_loop()
            def send_sync():
                with smtplib.SMTP_SSL(server, port, timeout=15) as smtp:
                    smtp.login(sender, auth_code)
                    smtp.sendmail(sender, [to_email], msg.as_string())
            await loop.run_in_executor(None, send_sync)
            return {"success": True, "msg": f"✅ 邮件已发送至 {to_email}"}
        except smtplib.SMTPAuthenticationError:
            return {"success": False, "msg": "❌ 登录失败：请检查邮箱地址和授权码是否正确，是否已开启IMAP/SMTP服务"}
        except Exception as e:
            logger.error(f"[EmailSender] 发送异常: {e}")
            return {"success": False, "msg": f"发送失败: {str(e)}"}


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.context = context

        try:
            self.data_dir = StarTools.get_data_dir("astrbot_plugin_qzone_tools")
        except RuntimeError:
            self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_qzone_tools")
        os.makedirs(self.data_dir, exist_ok=True)

        max_memories_per_user = self.config.get("max_memories_per_user", 100)
        self.memory_manager = MemoryManager(self.data_dir, max_memories_per_user)
        self.email_sender = EmailSender(self.config)

        self.db_manager = DatabaseManager(self.data_dir)
        self.session = QzoneSession()
        self.qzone = QzoneAPI(self.session)
        self._client = None
        self.scheduled_tasks: Dict[str, ScheduledTask] = {}
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self._groups_cache: List[dict] = []
        self._friends_cache: List[dict] = []
        self._cache_time = 0
        self._cache_expire = 300
        self._cache_lock = asyncio.Lock()
        self.status_manager = QQStatusManager()
        self.command_executor: Optional[ScheduledCommandExecutor] = None
        self._restored = False

        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_lock = asyncio.Lock()

    async def initialize(self):
        self.status_manager.set_db_manager(self.db_manager)
        self.command_executor = ScheduledCommandExecutor(self)
        asyncio.create_task(self.command_executor.start_periodic_check())
        asyncio.create_task(self._delayed_restore())
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        logger.info(f"[Main] 插件已加载")

    async def _periodic_refresh(self):
        while True:
            try:
                await asyncio.sleep(2 * 3600)
                await self._refresh_session()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Main] 定时刷新会话失败: {e}")

    async def _refresh_session(self):
        async with self._refresh_lock:
            client = await self._get_client()
            if not client:
                logger.warning("[Main] 无法获取QQ客户端，跳过会话刷新")
                return
            logger.info("[Main] 开始刷新QQ空间会话...")
            success = await self.session.initialize(client)
            if success:
                logger.info("[Main] QQ空间会话刷新成功")
            else:
                logger.warning("[Main] QQ空间会话刷新失败")

    async def _delayed_restore(self):
        await asyncio.sleep(10)
        try:
            if hasattr(self.context, 'platform_manager'):
                pm = self.context.platform_manager
                if hasattr(pm, 'get_insts'):
                    platforms = pm.get_insts()
                    for platform in platforms:
                        if hasattr(platform, 'get_client'):
                            self._client = platform.get_client()
                            break
                        elif hasattr(platform, 'client'):
                            self._client = platform.client
                            break
        except Exception as e:
            logger.warning(f"[Main] 获取客户端失败: {e}")
        if self._client:
            await self._do_restore()

    async def _do_restore(self):
        if self._restored:
            return
        try:
            if self._client:
                await self.status_manager.restore_from_db(self._client)
                await self.command_executor._check_and_execute_pending()
                self._restored = True
                logger.info("[Main] 数据恢复完成")
        except Exception as e:
            logger.error(f"[Main] 恢复失败: {e}")

    async def terminate(self):
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self.command_executor:
            self.command_executor.stop_periodic_check()
        for task_id, task in list(self.running_tasks.items()):
            if not task.done():
                task.cancel()
        for task_id, task in list(self.command_executor.running_tasks.items()):
            if not task.done():
                task.cancel()
        if self.status_manager.restore_task and not self.status_manager.restore_task.done():
            self.status_manager.restore_task.cancel()
        if self.status_manager.pending_task and not self.status_manager.pending_task.done():
            self.status_manager.pending_task.cancel()
        if self.status_manager.is_status_active() and self.db_manager:
            await self.db_manager.save_status(
                self.status_manager.current_status,
                self.status_manager.current_status_name,
                self.status_manager.status_end_time
            )
        logger.info("[Main] 插件已卸载")

    async def _get_client(self, event: AstrMessageEvent = None):
        if self._client and hasattr(self._client, 'call_action'):
            return self._client
        if event:
            client = getattr(event, 'bot', None)
            if client and hasattr(client, 'call_action'):
                self._client = client
                return client
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts()
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'call_action'):
                        self._client = client
                        return client
                elif hasattr(platform, 'client') and hasattr(platform.client, 'call_action'):
                    self._client = platform.client
                    return platform.client
        except Exception as e:
            logger.debug(f"[Main] 从 platform_manager 获取 client 失败: {e}")
        return None

    async def _ensure_initialized(self, event: AstrMessageEvent) -> bool:
        if self.session.initialized:
            return True
        client = await self._get_client(event)
        if not client:
            return False
        return await self.session.initialize(client)

    async def _update_contacts_cache(self, client):
        async with self._cache_lock:
            now = time.time()
            if now - self._cache_time < self._cache_expire and (self._groups_cache or self._friends_cache):
                return
            try:
                try:
                    groups_result = await client.call_action('get_group_list')
                    self._groups_cache = groups_result if isinstance(groups_result, list) else groups_result.get('data', [])
                except:
                    self._groups_cache = []
                try:
                    friends_result = await client.call_action('get_friend_list')
                    self._friends_cache = friends_result if isinstance(friends_result, list) else friends_result.get('data', [])
                except:
                    self._friends_cache = []
                self._cache_time = now
            except Exception as e:
                logger.error(f"[Main] 更新缓存失败: {e}")

    def _validate_target_id(self, target_id: str) -> Tuple[bool, str]:
        target_id = str(target_id).strip()
        if not target_id:
            return False, "目标ID不能为空"
        if not target_id.isdigit():
            return False, "目标ID必须是纯数字"
        return True, target_id

    def _parse_time(self, time_str: str) -> Optional[datetime]:
        time_str = time_str.strip()
        now = datetime.now()
        daily_match = re.match(r'每天的(\d{1,2}):(\d{2})', time_str)
        if daily_match:
            hour, minute = int(daily_match.group(1)), int(daily_match.group(2))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M"]
        for fmt in formats:
            try:
                parsed = datetime.strptime(time_str, fmt)
                if fmt == "%H:%M":
                    parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
                    if parsed <= now:
                        parsed += timedelta(days=1)
                elif fmt == "%m-%d %H:%M":
                    parsed = parsed.replace(year=now.year)
                    if parsed <= now:
                        parsed = parsed.replace(year=now.year + 1)
                return parsed
            except:
                continue
        return None

    async def _execute_scheduled_task(self, task_id: str, delay_seconds: float):
        try:
            await asyncio.sleep(delay_seconds)
            task = self.scheduled_tasks.get(task_id)
            if not task or task.cancelled:
                return
            client = self._client
            if not client:
                return
            if task.chat_type == "group":
                await client.call_action('send_group_msg', group_id=int(task.target_id), message=task.message)
            else:
                await client.call_action('send_private_msg', user_id=int(task.target_id), message=task.message)
            task.completed = True
        except Exception as e:
            logger.error(f"[Main] 定时任务执行失败: {e}")
        finally:
            if task_id in self.scheduled_tasks:
                del self.scheduled_tasks[task_id]
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]

    # ==================== 辅助函数：限制输出长度 ====================
    def _truncate_message(self, text: str, max_chars: int = None) -> str:
        """限制文本长度，避免超出模型上下文限制"""
        if max_chars is None:
            max_chars = self.config.get("max_output_chars", 2000)
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 100] + f"\n\n... (内容过长，已截断，共 {len(text)} 字符，仅显示前 {max_chars - 100} 字符)"

    # ==================== 辅助函数：获取群成员角色 ====================
    async def _get_group_member_role(self, group_id: str, user_id: str) -> str:
        client = await self._get_client()
        if not client:
            return "unknown"
        try:
            info = await client.call_action('get_group_member_info', group_id=int(group_id), user_id=int(user_id), no_cache=False)
            role = info.get('role', 'member')
            if role == 'owner':
                return '群主'
            elif role == 'admin':
                return '管理员'
            else:
                return '成员'
        except Exception as e:
            logger.debug(f"[Main] 获取群成员角色失败: {e}")
            return "unknown"

    # ==================== LLM 工具函数（记忆相关） ====================
    @filter.llm_tool(name="add_memory")
    async def add_memory(self, event: AstrMessageEvent, content: str, tags: str = "", importance: int = 5) -> dict:
        """添加重要记忆到存储中。AI 可以根据对话内容自动提取关键信息并保存，以便后续对话中回忆。
        
        Args:
            content(string): 记忆内容，必填，例如“用户喜欢喝咖啡”
            tags(string): 标签，多个标签用英文逗号分隔，例如“偏好,饮食”
            importance(number): 重要程度，1-10，数字越大越重要，默认5
        
        Returns:
            dict: 操作结果，包含 status 和 message
        """
        if not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供记忆内容。\n用法示例：添加记忆 今天天气很好，标签 日常，重要度 8"}
        user_id = event.get_sender_id()
        tags_list = [t.strip() for t in tags.split(",")] if tags else []
        importance = max(1, min(10, importance))
        memory_id = await self.memory_manager.add_memory(user_id, content, tags_list, importance)
        msg = f"✅ 记忆已保存\nID: {memory_id}\n内容: {content[:50]}{'...' if len(content)>50 else ''}"
        return {"status": "success", "message": self._truncate_message(msg)}

    @filter.llm_tool(name="search_memories")
    async def search_memories(self, event: AstrMessageEvent, keyword: str = "", user_specific: bool = True, limit: int = 10) -> dict:
        """搜索已保存的记忆。支持按关键词、用户范围筛选。
        
        Args:
            keyword(string): 搜索关键词，可选，不提供则返回最新记忆
            user_specific(boolean): 是否只搜索当前用户的记忆，默认为True
            limit(number): 返回结果数量限制，默认10，最大20
        
        Returns:
            dict: 记忆列表
        """
        user_id = event.get_sender_id() if user_specific else None
        limit = min(limit, 20)
        memories = await self.memory_manager.get_memories(user_id=user_id, keyword=keyword if keyword else None, limit=limit)
        if not memories:
            if keyword:
                return {"status": "success", "message": f"📭 未找到包含「{keyword}」的记忆"}
            return {"status": "success", "message": "📭 暂无记忆"}
        lines = [f"📚 找到 {len(memories)} 条记忆："]
        for i, m in enumerate(memories, 1):
            tags_str = f"[{', '.join(m.get('tags', []))}]" if m.get('tags') else ""
            content = m.get('content', '')[:40] + ('...' if len(m.get('content',''))>40 else '')
            lines.append(f"{i}. [{m['id']}] {content} (重要度:{m.get('importance',5)}) {tags_str} - {m.get('updated_at','')[:10]}")
        msg = "\n".join(lines)
        return {"status": "success", "message": self._truncate_message(msg)}

    @filter.llm_tool(name="update_memory")
    async def update_memory(self, event: AstrMessageEvent, memory_id: str, content: str = None, tags: str = None, importance: int = None) -> dict:
        """更新已有的记忆内容、标签或重要度。
        
        Args:
            memory_id(string): 记忆ID，必填（可从 search_memories 获取）
            content(string): 新的记忆内容，可选
            tags(string): 新的标签，多个用逗号分隔，可选
            importance(number): 新的重要程度1-10，可选
        
        Returns:
            dict: 更新结果
        """
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要更新的记忆ID。\n用法示例：更新记忆 abc123 内容 新内容 标签 重要 重要度 9"}
        existing = await self.memory_manager.get_memory_by_id(memory_id)
        if not existing:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        tags_list = [t.strip() for t in tags.split(",")] if tags is not None else None
        success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
        if success:
            return {"status": "success", "message": f"✅ 记忆已更新\nID: {memory_id}"}
        else:
            return {"status": "error", "message": "❌ 更新失败"}

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event: AstrMessageEvent, memory_id: str) -> dict:
        """删除指定的记忆。
        
        Args:
            memory_id(string): 要删除的记忆ID，必填
        
        Returns:
            dict: 删除结果
        """
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要删除的记忆ID。\n用法示例：删除记忆 abc123"}
        existing = await self.memory_manager.get_memory_by_id(memory_id)
        if not existing:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        success = await self.memory_manager.delete_memory(memory_id)
        if success:
            return {"status": "success", "message": f"🗑️ 记忆已删除\nID: {memory_id}"}
        else:
            return {"status": "error", "message": "❌ 删除失败"}

    @filter.llm_tool(name="get_memory_detail")
    async def get_memory_detail(self, event: AstrMessageEvent, memory_id: str) -> dict:
        """获取单条记忆的完整详情。
        
        Args:
            memory_id(string): 记忆ID，必填
        
        Returns:
            dict: 记忆的详细信息
        """
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供记忆ID。\n用法示例：查看记忆 abc123"}
        m = await self.memory_manager.get_memory_by_id(memory_id)
        if not m:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        lines = [
            f"📋 记忆详情",
            f"ID: {m['id']}",
            f"用户: {m['user_id']}",
            f"内容: {m['content']}",
            f"标签: {', '.join(m.get('tags', [])) or '无'}",
            f"重要度: {m.get('importance',5)}/10",
            f"创建: {m.get('created_at')}",
            f"更新: {m.get('updated_at')}"
        ]
        msg = "\n".join(lines)
        return {"status": "success", "message": self._truncate_message(msg)}

    # ==================== LLM 工具函数（消息发送等） ====================
    @filter.llm_tool(name="send_message")
    async def send_message_tool(self, event: AstrMessageEvent, target_id: str, message: str, chat_type: str = "auto") -> dict:
        """立即向指定的QQ好友或群聊发送文本消息。
        
        Args:
            target_id(string): 目标QQ号或群号，必填
            message(string): 要发送的消息内容，必填
            chat_type(string): 聊天类型，可选值：group(群聊)/private(私聊)/auto(自动识别)，默认auto
        
        Returns:
            dict: 发送结果
        """
        if not target_id or target_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标QQ号或群号。\n用法示例：发送消息 123456 你好"}
        if not message or message.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要发送的消息内容。\n用法示例：发送消息 123456 你好"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_id)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        if chat_type == "auto":
            await self._update_contacts_cache(client)
            is_group = any(str(g.get('group_id')) == target_id for g in self._groups_cache)
            chat_type = "group" if is_group else "private"
        try:
            if chat_type == "group":
                await client.call_action('send_group_msg', group_id=int(target_id), message=message)
            else:
                await client.call_action('send_private_msg', user_id=int(target_id), message=message)
            return {"status": "success", "message": f"✅ 已发送消息到 {target_id}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {str(e)}"}

    @filter.llm_tool(name="schedule_message")
    async def schedule_message(self, event: AstrMessageEvent, target_id: str, message: str, send_time: str, chat_type: str = "group") -> dict:
        """创建简单的定时消息任务（仅发送文本消息，重启后丢失）。如需更复杂的功能（发空间、改状态、LLM提醒等），请使用 create_scheduled_command。
        
        Args:
            target_id(string): 目标QQ号或群号，必填
            message(string): 要发送的消息内容，必填
            send_time(string): 发送时间，支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM，必填
            chat_type(string): 聊天类型，group(群聊)或private(私聊)，默认group
        
        Returns:
            dict: 任务创建结果
        """
        if not target_id or target_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标QQ号或群号。\n用法示例：定时消息 123456 明天见 明天08:00"}
        if not message or message.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要发送的消息内容。\n用法示例：定时消息 123456 明天见 明天08:00"}
        if not send_time or send_time.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供发送时间。\n支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_id)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        parsed_time = self._parse_time(send_time)
        if not parsed_time:
            return {"status": "error", "message": "错误：无法理解时间格式，请使用如 明天08:00、2026-01-01 12:00、每天的08:00"}
        if parsed_time <= datetime.now():
            return {"status": "error", "message": "错误：指定的时间已经过去"}
        task_id = str(uuid.uuid4())[:8]
        task = ScheduledTask(task_id=task_id, target_id=target_id, message=message, send_time=parsed_time, chat_type=chat_type)
        self.scheduled_tasks[task_id] = task
        delay_seconds = (parsed_time - datetime.now()).total_seconds()
        asyncio_task = asyncio.create_task(self._execute_scheduled_task(task_id, delay_seconds))
        self.running_tasks[task_id] = asyncio_task
        msg = f"✅ 定时任务已创建\n任务ID: {task_id}\n时间: {parsed_time.strftime('%Y-%m-%d %H:%M:%S')}\n⚠️ 注意：此任务重启后丢失，如需持久化请使用 create_scheduled_command"
        return {"status": "success", "message": self._truncate_message(msg)}

    @filter.llm_tool(name="cancel_scheduled_message")
    async def cancel_scheduled_message(self, event: AstrMessageEvent, task_id: str) -> dict:
        """取消由 schedule_message 创建的定时消息任务。
        
        Args:
            task_id(string): 要取消的任务ID，必填
        
        Returns:
            dict: 取消结果
        """
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供任务ID。\n用法示例：取消定时任务 abc123"}
        if task_id not in self.scheduled_tasks:
            return {"status": "error", "message": f"错误：未找到任务 {task_id}"}
        task = self.scheduled_tasks[task_id]
        task.cancelled = True
        if task_id in self.running_tasks:
            self.running_tasks[task_id].cancel()
            del self.running_tasks[task_id]
        if task_id in self.scheduled_tasks:
            del self.scheduled_tasks[task_id]
        return {"status": "success", "message": f"✅ 已取消任务 {task_id}"}

    @filter.llm_tool(name="list_scheduled_messages")
    async def list_scheduled_messages(self, event: AstrMessageEvent, show_all: bool = False) -> dict:
        """列出由 schedule_message 创建的定时消息任务。
        
        Args:
            show_all(boolean): 是否显示所有任务（包括已完成和已取消的），默认False
        
        Returns:
            dict: 任务列表
        """
        tasks = list(self.scheduled_tasks.values()) if show_all else [t for t in self.scheduled_tasks.values() if not t.cancelled and not t.completed]
        if not tasks:
            return {"status": "success", "message": "当前没有定时消息任务"}
        lines = [f"📋 定时消息任务列表（{len(tasks)}个）"]
        for t in sorted(tasks, key=lambda x: x.send_time):
            status = "✅" if t.completed else "❌" if t.cancelled else "⏳"
            lines.append(f"{status} [{t.task_id}] {t.send_time.strftime('%m-%d %H:%M')} -> {t.target_id}")
        msg = "\n".join(lines)
        return {"status": "success", "message": self._truncate_message(msg)}

    @filter.llm_tool(name="publish_qzone")
    async def publish_qzone(self, event: AstrMessageEvent, content: str) -> dict:
        """发布QQ空间说说（需要机器人已登录且支持QQ空间操作）。
        
        Args:
            content(string): 说说内容，必填
        
        Returns:
            dict: 发布结果
        """
        if not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供说说内容。\n用法示例：发表空间说说 今天天气真好"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        success = await self.session.initialize(client)
        if not success:
            return {"status": "error", "message": "错误：无法初始化QQ空间，请检查网络或重新登录"}
        result = await self.qzone.publish_post(content)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    @filter.llm_tool(name="send_poke")
    async def send_poke(self, event: AstrMessageEvent, target_qq: str, chat_type: str = "auto") -> dict:
        """发送戳一戳（窗口抖动）。
        
        Args:
            target_qq(string): 目标QQ号，必填
            chat_type(string): 聊天类型，可选值：group(群聊)/private(私聊)/auto(自动识别)，默认auto
        
        Returns:
            dict: 发送结果
        """
        if not target_qq or target_qq.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标QQ号。\n用法示例：戳一戳 123456"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_qq)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        if chat_type == "auto":
            chat_type = "private" if event.is_private_chat() else "group"
        try:
            if chat_type == "private":
                await client.call_action('friend_poke', user_id=int(target_qq))
            else:
                group_id = event.get_group_id()
                if not group_id:
                    return {"status": "error", "message": "错误：无法获取群号"}
                await client.call_action('group_poke', group_id=int(group_id), user_id=int(target_qq))
            return {"status": "success", "message": f"✅ 已戳一戳 {target_qq}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {str(e)}"}

    @filter.llm_tool(name="update_qq_status")
    async def update_qq_status(self, event: AstrMessageEvent, status: str, duration_minutes: int, delay_minutes: int = 0) -> dict:
        """设置QQ在线状态（支持基础状态和娱乐状态）。
        
        Args:
            status(string): 状态码，必填。可选值：online(在线), qme(Q我吧), away(离开), busy(忙碌), dnd(请勿打扰), invisible(隐身), listening(听歌中), sleeping(睡觉中), studying(学习中)
            duration_minutes(number): 状态持续时间（分钟），必填，到期后自动恢复为“在线”
            delay_minutes(number): 延迟执行时间（分钟），默认0
        
        Returns:
            dict: 设置结果
        """
        if not status or status.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供状态码。\n可用状态：online, qme, away, busy, dnd, invisible, listening, sleeping, studying"}
        if duration_minutes is None:
            return {"status": "error", "message": "❌ 参数缺失：请提供持续时间（分钟）。\n用法示例：设置QQ状态 listening 30"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        if duration_minutes < 1:
            duration_minutes = 1
        result = await self.status_manager.set_status(client, status, duration_minutes, delay_minutes)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    @filter.llm_tool(name="get_qq_status")
    async def get_qq_status(self, event: AstrMessageEvent) -> dict:
        """获取当前QQ在线状态描述（包含剩余时间）。"""
        return {"status": "success", "message": self.status_manager.get_current_status_desc()}

    @filter.llm_tool(name="get_fun_status_list")
    async def get_fun_status_list(self, event: AstrMessageEvent) -> dict:
        """获取可用的娱乐状态列表。"""
        return {"status": "success", "message": "娱乐状态：listening(听歌中), sleeping(睡觉中), studying(学习中)"}

    @filter.llm_tool(name="create_scheduled_command")
    async def create_scheduled_command(self, event: AstrMessageEvent, command_type: str, execute_time: str, params: str, recurrence: str = "once") -> dict:
        """【高级定时指令】持久化存储，支持重启恢复，可执行多种操作：qzone_post（发空间）、status_change（改状态）、llm_remind（LLM提醒）。注意：如需简单定时发消息，请使用 schedule_message。
        
        Args:
            command_type(string): 指令类型，必填。可选值：qzone_post(发说说), status_change(改状态), llm_remind(LLM提醒)
            execute_time(string): 执行时间，必填。支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM
            params(string): 指令参数，JSON格式字符串，必填。例如：{"content": "晚安"}
            recurrence(string): 重复类型，可选值：once(单次)/daily(每天)，默认once
        
        Returns:
            dict: 创建结果
        """
        if not command_type or command_type.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供指令类型。\n可选：qzone_post, status_change, llm_remind"}
        if not execute_time or execute_time.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供执行时间。\n支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM"}
        if not params or params.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供指令参数（JSON格式）。\n例如：{\"content\": \"晚安\"}"}
        parsed_time = self._parse_time(execute_time)
        if not parsed_time:
            return {"status": "error", "message": "错误：无法理解时间格式"}
        if parsed_time <= datetime.now():
            return {"status": "error", "message": "❌ 执行时间不能早于当前时间"}
        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError:
            return {"status": "error", "message": "错误：params必须是有效JSON字符串"}
        valid_types = ["qzone_post", "status_change", "llm_remind"]
        if command_type not in valid_types:
            return {"status": "error", "message": f"错误：无效类型，可选: {', '.join(valid_types)}"}
        session_info = None
        if command_type == "llm_remind":
            session_info = {
                'unified_msg_origin': event.unified_msg_origin,
                'platform_name': event.get_platform_name(),
                'sender_id': event.get_sender_id(),
                'sender_name': event.get_sender_name()
            }
        task_id = str(uuid.uuid4())[:8]
        success = await self.db_manager.save_scheduled_command(task_id, command_type, params_dict, parsed_time, recurrence, session_info)
        if success:
            return {"status": "success", "message": f"✅ 定时指令已创建\n任务ID: {task_id}\n此指令持久化存储，重启后仍会执行。"}
        else:
            return {"status": "error", "message": "❌ 保存失败"}

    @filter.llm_tool(name="list_scheduled_commands")
    async def list_scheduled_commands(self, event: AstrMessageEvent, include_executed: bool = False) -> dict:
        """列出由 create_scheduled_command 创建的定时指令任务。
        
        Args:
            include_executed(boolean): 是否包含已执行的指令，默认False
        
        Returns:
            dict: 指令列表
        """
        commands = await self.db_manager.get_all_commands(include_executed)
        if not commands:
            return {"status": "success", "message": "当前没有定时指令任务"}
        lines = [f"📋 定时指令列表（{len(commands)}条）"]
        for cmd in commands[:15]:
            status_map = {0: "⏳", 1: "✅", 2: "❌", -1: "⚠️"}
            status = status_map.get(cmd.get('executed'), "❓")
            time_str = datetime.fromisoformat(cmd['execute_time']).strftime("%m-%d %H:%M")
            lines.append(f"{status} [{cmd['id']}] {cmd['command_type']} {time_str}")
        msg = "\n".join(lines)
        return {"status": "success", "message": self._truncate_message(msg)}

    @filter.llm_tool(name="cancel_scheduled_command")
    async def cancel_scheduled_command(self, event: AstrMessageEvent, task_id: str) -> dict:
        """取消由 create_scheduled_command 创建的定时指令任务（标记为已取消，不再执行）。
        
        Args:
            task_id(string): 要取消的任务ID，必填
        
        Returns:
            dict: 取消结果
        """
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要取消的任务ID。\n用法示例：取消指令 abc123"}
        await self.db_manager.cancel_command(task_id)
        self.command_executor.cancel_task(task_id)
        return {"status": "success", "message": f"✅ 已取消指令 {task_id}"}

    @filter.llm_tool(name="delete_scheduled_command")
    async def delete_scheduled_command(self, event: AstrMessageEvent, task_id: str) -> dict:
        """彻底删除由 create_scheduled_command 创建的定时指令任务（从数据库中移除）。
        
        Args:
            task_id(string): 要删除的任务ID，必填
        
        Returns:
            dict: 删除结果
        """
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要删除的任务ID。\n用法示例：删除指令 abc123"}
        await self.db_manager.delete_command(task_id)
        self.command_executor.cancel_task(task_id)
        return {"status": "success", "message": f"✅ 已删除指令 {task_id}"}

    @filter.llm_tool(name="recall_by_reply")
    async def recall_by_reply(self, event: AiocqhttpMessageEvent) -> dict:
        """通过引用消息撤回群聊消息。使用时需要引用要撤回的消息（回复时勾选引用）。仅支持QQ群聊。
        
        Returns:
            dict: 撤回结果
        """
        if event.is_private_chat():
            return {"status": "error", "message": "❌ 此功能仅支持群聊中使用"}
        chain = event.get_messages()
        if not chain or len(chain)==0 or not isinstance(chain[0], Reply):
            return {"status": "error", "message": "❌ 请引用要撤回的消息（回复消息时勾选引用）"}
        msg_id = str(chain[0].id)
        if not msg_id.isdigit():
            return {"status": "error", "message": "❌ 引用的消息ID无效"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "❌ 无法获取群号"}
        try:
            await event.bot.delete_msg(message_id=int(msg_id), group_id=int(group_id))
            return {"status": "success", "message": f"✅ 撤回成功\n• 消息ID: {msg_id}"}
        except Exception as e:
            return {"status": "error", "message": f"❌ 撤回失败: {str(e)[:200]}"}

    @filter.llm_tool(name="send_qq_email")
    async def send_qq_email_tool(self, event: AstrMessageEvent, to: str, subject: str, content: str, nickname: str = "") -> dict:
        """通过QQ邮箱SMTP服务发送电子邮件。需要先在插件配置中设置发件人邮箱和授权码。
        
        Args:
            to(string): 收件人邮箱地址，必填
            subject(string): 邮件主题，必填
            content(string): 邮件正文内容，必填
            nickname(string): 发件人昵称，可选
        
        Returns:
            dict: 发送结果
        """
        if not to or to.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供收件人邮箱地址。\n用法示例：发送邮件 friend@qq.com 测试 你好"}
        if not subject or subject.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供邮件主题。\n用法示例：发送邮件 friend@qq.com 测试 你好"}
        if not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供邮件正文内容。\n用法示例：发送邮件 friend@qq.com 测试 你好"}
        result = await self.email_sender.send_email(to, subject, content, nickname)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    # ==================== LLM 工具函数：查询群成员身份 ====================
    @filter.llm_tool(name="get_user_group_role")
    async def get_user_group_role(self, event: AiocqhttpMessageEvent, group_id: str, user_id: str) -> dict:
        """查询指定用户在指定QQ群中的身份（群主/管理员/成员）。
        
        Args:
            group_id(string): 群号，必填
            user_id(string): 用户QQ号，必填
        
        Returns:
            dict: 身份信息
        """
        if not group_id or not group_id.strip():
            return {"status": "error", "message": "❌ 参数缺失：请提供群号。\n用法示例：查询群成员身份 123456 789012"}
        if not user_id or not user_id.strip():
            return {"status": "error", "message": "❌ 参数缺失：请提供用户QQ号。\n用法示例：查询群成员身份 123456 789012"}
        if not group_id.isdigit() or not user_id.isdigit():
            return {"status": "error", "message": "❌ 群号和用户QQ号必须为纯数字"}
        role = await self._get_group_member_role(group_id, user_id)
        if role == "unknown":
            return {"status": "error", "message": f"无法查询用户 {user_id} 在群 {group_id} 的身份，请确认机器人是否在群内且有权限。"}
        return {"status": "success", "message": f"用户 {user_id} 在群 {group_id} 中的身份是：{role}"}

    # ==================== LLM 工具函数：群管理（按照示例插件逻辑） ====================
    @filter.llm_tool(name="set_essence_msg")
    async def set_essence_msg(self, event: AiocqhttpMessageEvent) -> dict:
        """将引用消息添加到群精华。使用时需要引用要设置精华的消息。"""
        first_seg = event.get_messages()[0]
        if isinstance(first_seg, Reply):
            try:
                await event.bot.set_essence_msg(message_id=int(first_seg.id))
                msg = f"已将消息 {first_seg.id} 添加到群精华"
                return {"status": "success", "message": msg}
            except Exception as e:
                return {"status": "error", "message": f"设置精华失败: {str(e)}"}
        else:
            return {"status": "error", "message": "请引用要设置为精华的消息"}

    @filter.llm_tool(name="delete_essence_msg")
    async def delete_essence_msg(self, event: AiocqhttpMessageEvent) -> dict:
        """将引用消息从群精华中移除。使用时需要引用要取消精华的消息。"""
        first_seg = event.get_messages()[0]
        if isinstance(first_seg, Reply):
            try:
                await event.bot.delete_essence_msg(message_id=int(first_seg.id))
                msg = f"已将消息 {first_seg.id} 移出群精华"
                return {"status": "success", "message": msg}
            except Exception as e:
                return {"status": "error", "message": f"取消精华失败: {str(e)}"}
        else:
            return {"status": "error", "message": "请引用要取消精华的消息"}

    @filter.llm_tool(name="set_group_ban")
    async def set_group_ban(self, event: AiocqhttpMessageEvent, user_id: str, duration: int) -> dict:
        """禁言或解禁指定用户。duration为禁言秒数（必须是60的倍数），设置为0即解除禁言。
        
        Args:
            user_id(string): 要禁言/解禁的用户QQ号
            duration(number): 禁言持续时间（秒），0表示解禁
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_ban(group_id=int(group_id), user_id=int(user_id), duration=duration)
            if duration == 0:
                msg = f"已解禁用户 {user_id}"
            else:
                minutes = duration // 60
                msg = f"已禁言用户 {user_id}，时长 {minutes} 分钟"
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"禁言操作失败: {str(e)}"}

    @filter.llm_tool(name="set_group_kick")
    async def set_group_kick(self, event: AiocqhttpMessageEvent, user_id: str) -> dict:
        """将用户从群聊中移除。需要开启踢人功能（kick_enabled=true）。
        
        Args:
            user_id(string): 要踢出的用户QQ号
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        if not self.config.get("kick_enabled", True):
            return {"status": "error", "message": "❌ 踢人功能已被管理员禁用（kick_enabled=false）"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_kick(group_id=int(group_id), user_id=int(user_id), reject_add_request=False)
            return {"status": "success", "message": f"已踢出用户 {user_id}"}
        except Exception as e:
            return {"status": "error", "message": f"踢人失败: {str(e)}"}

    @filter.llm_tool(name="set_group_whole_ban")
    async def set_group_whole_ban(self, event: AiocqhttpMessageEvent, enable: bool) -> dict:
        """开启或关闭全体禁言。
        
        Args:
            enable(boolean): True开启全体禁言，False关闭
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_whole_ban(group_id=int(group_id), enable=enable)
            action = "开启" if enable else "关闭"
            return {"status": "success", "message": f"已{action}全体禁言"}
        except Exception as e:
            return {"status": "error", "message": f"全体禁言操作失败: {str(e)}"}

    @filter.llm_tool(name="set_group_card")
    async def set_group_card(self, event: AiocqhttpMessageEvent, user_id: str, card: str) -> dict:
        """修改群成员的群昵称（群名片）。card为空字符串时取消群昵称。
        
        Args:
            user_id(string): 要修改群昵称的用户QQ号
            card(string): 新的群昵称，为空则取消
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_card(group_id=int(group_id), user_id=int(user_id), card=card)
            if card:
                msg = f"已将用户 {user_id} 的群昵称修改为：{card}"
            else:
                msg = f"已取消用户 {user_id} 的群昵称"
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"修改群昵称失败: {str(e)}"}

    @filter.llm_tool(name="send_group_notice")
    async def send_group_notice(self, event: AiocqhttpMessageEvent, content: str) -> dict:
        """发布群公告。
        
        Args:
            content(string): 公告内容
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot._send_group_notice(group_id=int(group_id), content=content)
            return {"status": "success", "message": f"群公告已发布：{content}"}
        except Exception as e:
            return {"status": "error", "message": f"发布公告失败: {str(e)}"}

    @filter.llm_tool(name="delete_group_notice")
    async def delete_group_notice(self, event: AiocqhttpMessageEvent, notice_id: str) -> dict:
        """撤回群公告。
        
        Args:
            notice_id(string): 公告ID
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot._del_group_notice(group_id=int(group_id), notice_id=notice_id)
            return {"status": "success", "message": f"已撤回公告 {notice_id}"}
        except Exception as e:
            return {"status": "error", "message": f"撤回公告失败: {str(e)}"}

    @filter.llm_tool(name="list_group_files")
    async def list_group_files(self, event: AiocqhttpMessageEvent) -> dict:
        """查询群文件列表（根目录）。返回文件名和大小，受 max_output_chars 限制。"""
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            result = await event.bot.get_group_root_files(group_id=int(group_id))
            files = result.get('files', [])
            if not files:
                return {"status": "success", "message": f"群 {group_id} 根目录下没有文件"}
            lines = [f"群 {group_id} 根目录文件列表："]
            for f in files[:20]:
                name = f.get('file_name', '未知')
                size = f.get('file_size', 0)
                size_mb = size / (1024 * 1024)
                lines.append(f"  • {name} ({size_mb:.2f} MB) [file_id: {f.get('file_id', 'N/A')}]")
            if len(files) > 20:
                lines.append(f"  ... 共 {len(files)} 个文件，仅显示前20个")
            msg = "\n".join(lines)
            return {"status": "success", "message": self._truncate_message(msg)}
        except Exception as e:
            return {"status": "error", "message": f"查询文件失败: {str(e)}"}

    @filter.llm_tool(name="delete_group_file")
    async def delete_group_file(self, event: AiocqhttpMessageEvent, file_id: str = None) -> dict:
        """删除群文件。可通过 file_id 指定要删除的文件，或通过引用文件消息自动获取 file_id。
        
        Args:
            file_id(string): 文件ID，可选。如果不提供，则尝试从引用的消息中提取文件ID。
        """
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            # 如果没有提供 file_id，尝试从引用消息中获取
            if not file_id:
                chain = event.get_messages()
                if chain and isinstance(chain[0], Reply):
                    # 引用消息，需要从原始消息中提取文件ID，这比较复杂，暂时要求用户提供 file_id
                    return {"status": "error", "message": "请提供要删除的 file_id（可通过 list_group_files 获取），当前不支持自动从引用中提取文件ID。"}
                else:
                    return {"status": "error", "message": "❌ 参数缺失：请提供 file_id，例如 /delete_group_file <file_id>"}
            await event.bot.delete_group_file(group_id=int(group_id), file_id=file_id)
            return {"status": "success", "message": f"✅ 已删除群文件 {file_id}"}
        except Exception as e:
            return {"status": "error", "message": f"删除文件失败: {str(e)}"}

    @filter.llm_tool(name="get_group_members_info")
    async def get_group_members_info(self, event: AiocqhttpMessageEvent) -> dict:
        """获取当前群聊的成员信息列表（包含user_id、display_name、username、role）。需要在禁言、踢人前调用以获取目标用户的ID。结果受 max_output_chars 限制。"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "这不是群聊"}
            members = await event.bot.get_group_member_list(group_id=int(group_id))
            if not members:
                return {"status": "error", "message": "获取群成员信息失败"}
            processed = []
            for m in members:
                processed.append({
                    "user_id": str(m.get("user_id", "")),
                    "display_name": m.get("card") or m.get("nickname") or f"用户{m.get('user_id')}",
                    "username": m.get("nickname") or f"用户{m.get('user_id')}",
                    "role": m.get("role", "member")
                })
            result_json = json.dumps({
                "group_id": group_id,
                "member_count": len(processed),
                "members": processed
            }, ensure_ascii=False, indent=2)
            truncated = self._truncate_message(result_json)
            return {"status": "success", "message": truncated}
        except Exception as e:
            return {"status": "error", "message": f"获取成员信息失败: {str(e)}"}

    # ==================== 联系人搜索与列表工具 ====================
    @filter.llm_tool(name="search_contacts")
    async def search_contacts(self, event: AstrMessageEvent, keyword: str = "", search_type: str = "all") -> dict:
        """搜索QQ好友或群聊，支持按QQ号、昵称、群名模糊匹配。search_type可选：all(全部)/friend(好友)/group(群聊)
        
        Args:
            keyword(string): 搜索关键词，必填（可以是QQ号、昵称或群名的一部分）
            search_type(string): 搜索范围，可选值：all/friend/group，默认all
        
        Returns:
            dict: 搜索结果列表
        """
        if not keyword or keyword.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供搜索关键词（可以是QQ号、昵称或群名的一部分）。\n用法示例：搜索联系人 张三 all"}
        if not self.config.get("search_enabled", True):
            return {"status": "error", "message": "联系人搜索功能已禁用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        await self._update_contacts_cache(client)
        max_chars = self.config.get("search_max_chars", 800)
        results = []
        keyword_lower = keyword.lower().strip()
        if search_type in ("all", "friend"):
            for friend in self._friends_cache:
                user_id = str(friend.get('user_id', ''))
                nickname = friend.get('nickname', '')
                remark = friend.get('remark', '')
                match = False
                if keyword_lower:
                    if keyword_lower in user_id or keyword_lower in nickname.lower() or keyword_lower in remark.lower():
                        match = True
                else:
                    match = True
                if match:
                    display_name = remark if remark else nickname
                    results.append(f"👤 好友 | {user_id} | {display_name}")
        if search_type in ("all", "group"):
            for group in self._groups_cache:
                group_id = str(group.get('group_id', ''))
                group_name = group.get('group_name', '')
                match = False
                if keyword_lower:
                    if keyword_lower in group_id or keyword_lower in group_name.lower():
                        match = True
                else:
                    match = True
                if match:
                    results.append(f"👥 群聊 | {group_id} | {group_name}")
        if not results:
            return {"status": "success", "message": f"未找到与「{keyword}」相关的联系人"}
        output = f"📇 搜索结果（共{len(results)}项）：\n" + "\n".join(results)
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n... (仅显示部分，共{len(results)}项，建议使用更精确的关键词)"
        return {"status": "success", "message": self._truncate_message(output)}

    @filter.llm_tool(name="list_contacts")
    async def list_contacts(self, event: AstrMessageEvent, contact_type: str = "all", limit: int = 20) -> dict:
        """获取好友或群聊列表（不进行模糊搜索，直接列出）。contact_type可选：all/friend/group
        
        Args:
            contact_type(string): 类型，可选值：all/friend/group，默认all
            limit(number): 返回的最大数量，默认20，最大100
        
        Returns:
            dict: 联系人列表
        """
        if not self.config.get("search_enabled", True):
            return {"status": "error", "message": "联系人列表功能已禁用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        await self._update_contacts_cache(client)
        max_chars = self.config.get("search_max_chars", 800)
        limit = min(limit, 100)
        lines = []
        if contact_type in ("all", "friend"):
            friends = self._friends_cache[:limit]
            for f in friends:
                user_id = f.get('user_id', '')
                name = f.get('remark') or f.get('nickname', '')
                lines.append(f"👤 {user_id} | {name}")
            if contact_type == "friend":
                lines.insert(0, f"📋 好友列表（共{len(self._friends_cache)}，显示{len(friends)}）：")
        if contact_type in ("all", "group"):
            groups = self._groups_cache[:limit]
            for g in groups:
                group_id = g.get('group_id', '')
                name = g.get('group_name', '')
                lines.append(f"👥 {group_id} | {name}")
            if contact_type == "group":
                lines.insert(0, f"📋 群聊列表（共{len(self._groups_cache)}，显示{len(groups)}）：")
        if not lines:
            return {"status": "success", "message": "暂无联系人数据"}
        output = "\n".join(lines)
        if len(output) > max_chars:
            output = output[:max_chars] + "\n... (内容过长已截断)"
        return {"status": "success", "message": self._truncate_message(output)}

    # ==================== 管理员指令（全部恢复） ====================
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_all_help")
    async def admin_all_help(self, event: AstrMessageEvent):
        """总帮助入口，展示所有管理员命令及用法"""
        help_text = """【AstrBot 插件管理命令总览】

/tool_memory - 记忆管理
  子命令: list [user_id], add <内容> [标签] [重要度], delete <ID>, update <ID> [新内容] [新标签] [重要度], get <ID>

/tool_send_message <目标ID> <消息内容> [chat_type]
  立即发送消息（chat_type: group/private/auto）

/tool_schedule <目标ID> <消息内容> <时间> [chat_type]
  简单定时消息（重启后丢失）

/tool_publish_qzone <说说内容>
  发布QQ空间说说

/tool_status <状态> <持续分钟> [延迟分钟]
  设置QQ在线状态（状态: online/qme/away/busy/dnd/invisible/listening/sleeping/studying）

/tool_status_get
  获取当前QQ在线状态

/tool_poke <目标QQ> [chat_type]
  发送戳一戳

/tool_recall
  引用消息撤回（仅QQ群聊）

/tool_email <收件人> <主题> <内容> [昵称]
  发送QQ邮箱邮件

/tool_scheduled_list [include_executed]
  列出定时指令（持久化）

/tool_scheduled_cancel <任务ID>
  取消定时指令

/tool_scheduled_delete <任务ID>
  彻底删除定时指令

/tool_search <关键词> [类型]  类型: all/friend/group
  搜索联系人

/tool_list [类型] [limit]  类型: all/friend/group
  列出联系人

--- 群管理命令（需 group_manage_enabled=true） ---
/ban_user <QQ号> <禁言分钟>   (群号自动获取)
/unban_user <QQ号>
/kick <QQ号>   (需 kick_enabled=true)
/whole_ban <on/off>
/set_card <QQ号> <新群昵称>   (空字符串取消)
/send_notice <公告内容>
/del_notice <公告ID>
/list_files
/group_members   (获取群成员列表)
/delete_group_file <file_id>   (删除群文件)

/tool_all_help
  显示本帮助
"""
        await event.send(MessageChain().message(help_text))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_memory")
    async def admin_memory(self, event: AstrMessageEvent):
        """管理员记忆管理命令：/tool_memory list/add/delete/update/get"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message(
                "用法：\n/tool_memory list [user_id] - 列出所有记忆\n/tool_memory add <内容> [标签用逗号分隔] [重要度1-10] - 添加记忆\n/tool_memory delete <记忆ID> - 删除记忆\n/tool_memory update <记忆ID> [内容] [标签] [重要度] - 更新记忆\n/tool_memory get <记忆ID> - 查看详情"
            ))
            return
        sub = args[1].lower()
        if sub == "list":
            user_id = args[2] if len(args) > 2 else None
            memories = await self.memory_manager.get_memories(user_id=user_id, limit=50)
            if not memories:
                await event.send(MessageChain().message("暂无记忆"))
                return
            lines = [f"📚 记忆列表（共{len(memories)}条）"]
            for m in memories:
                lines.append(f"{m['id']} | {m['user_id']} | {m['content'][:30]} | 重要:{m.get('importance',5)}")
            await event.send(MessageChain().message("\n".join(lines)))
        elif sub == "add":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory add <内容> [标签] [重要度]"))
                return
            content = args[2]
            tags = args[3] if len(args) > 3 else ""
            importance = int(args[4]) if len(args) > 4 and args[4].isdigit() else 5
            tags_list = [t.strip() for t in tags.split(",")] if tags else []
            memory_id = await self.memory_manager.add_memory("admin", content, tags_list, importance)
            await event.send(MessageChain().message(f"✅ 记忆已添加，ID: {memory_id}"))
        elif sub == "delete":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory delete <记忆ID>"))
                return
            memory_id = args[2]
            success = await self.memory_manager.delete_memory(memory_id)
            await event.send(MessageChain().message(f"✅ 记忆已删除" if success else "❌ 删除失败"))
        elif sub == "update":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory update <记忆ID> [新内容] [新标签] [新重要度]"))
                return
            memory_id = args[2]
            content = args[3] if len(args) > 3 else None
            tags = args[4] if len(args) > 4 else None
            importance = int(args[5]) if len(args) > 5 and args[5].isdigit() else None
            tags_list = [t.strip() for t in tags.split(",")] if tags is not None else None
            success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
            await event.send(MessageChain().message(f"✅ 记忆已更新" if success else "❌ 更新失败"))
        elif sub == "get":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory get <记忆ID>"))
                return
            m = await self.memory_manager.get_memory_by_id(args[2])
            if not m:
                await event.send(MessageChain().message("未找到记忆"))
                return
            lines = [f"ID: {m['id']}", f"用户: {m['user_id']}", f"内容: {m['content']}", f"标签: {', '.join(m.get('tags',[]))}", f"重要度: {m.get('importance',5)}", f"创建: {m.get('created_at')}", f"更新: {m.get('updated_at')}"]
            await event.send(MessageChain().message("\n".join(lines)))
        else:
            await event.send(MessageChain().message("未知子命令，可用: list, add, delete, update, get"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_send_message")
    async def admin_send_message(self, event: AstrMessageEvent):
        """管理员指令：立即发送消息到指定好友或群聊"""
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/tool_send_message <目标ID> <消息内容> [chat_type]"))
            return
        target_id = args[1]
        message = args[2]
        chat_type = "auto"
        result = await self.send_message_tool(event, target_id, message, chat_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_schedule")
    async def admin_schedule(self, event: AstrMessageEvent):
        """管理员指令：创建简单定时消息（重启后丢失）"""
        args = event.message_str.strip().split(maxsplit=3)
        if len(args) < 4:
            await event.send(MessageChain().message("用法：/tool_schedule <目标ID> <消息内容> <时间> [chat_type] 时间格式: YYYY-MM-DD HH:MM 或 HH:MM 或 每天的HH:MM"))
            return
        target_id = args[1]
        message = args[2]
        time_str = args[3]
        chat_type = "group"
        result = await self.schedule_message(event, target_id, message, time_str, chat_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_publish_qzone")
    async def admin_publish_qzone(self, event: AstrMessageEvent):
        """管理员指令：发布QQ空间说说"""
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_publish_qzone <说说内容>"))
            return
        content = args[1]
        result = await self.publish_qzone(event, content)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_status")
    async def admin_status(self, event: AstrMessageEvent):
        """管理员指令：设置QQ在线状态"""
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/tool_status <状态> <持续分钟> [延迟分钟]"))
            return
        status = args[1]
        duration = int(args[2]) if args[2].isdigit() else 30
        delay = int(args[3]) if len(args) > 3 and args[3].isdigit() else 0
        result = await self.update_qq_status(event, status, duration, delay)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_status_get")
    async def admin_status_get(self, event: AstrMessageEvent):
        """管理员指令：获取当前QQ在线状态"""
        result = self.status_manager.get_current_status_desc()
        await event.send(MessageChain().message(result))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_poke")
    async def admin_poke(self, event: AstrMessageEvent):
        """管理员指令：发送戳一戳"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_poke <目标QQ> [chat_type]"))
            return
        target = args[1]
        chat_type = args[2] if len(args) > 2 else "auto"
        result = await self.send_poke(event, target, chat_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_recall")
    async def admin_recall(self, event: AiocqhttpMessageEvent):
        """管理员指令：通过引用消息撤回（仅QQ群聊）"""
        result = await self.recall_by_reply(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_email")
    async def admin_email(self, event: AstrMessageEvent):
        """管理员指令：发送QQ邮箱邮件"""
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            sender = self.config.get("email_sender", "")
            auth_code = self.config.get("email_authorization_code", "")
            status = f"当前配置：发件人={sender if sender else '未设置'}，授权码={'已设置' if auth_code else '未设置'}"
            await event.send(MessageChain().message(
                f"用法：/tool_email <收件人> <主题> <内容> [昵称]\n注意：主题和内容中不要包含未转义的特殊字符\n{status}"
            ))
            return
        rest = parts[1]
        import shlex
        try:
            argv = shlex.split(rest)
        except:
            argv = rest.split()
        if len(argv) < 3:
            await event.send(MessageChain().message("用法：/tool_email <收件人> <主题> <内容> [昵称]"))
            return
        to = argv[0]
        subject = argv[1]
        content = argv[2]
        nickname = argv[3] if len(argv) > 3 else ""

        sender_cfg = self.config.get("email_sender", "")
        auth_cfg = self.config.get("email_authorization_code", "")
        if not sender_cfg or not auth_cfg:
            await event.send(MessageChain().message("❌ 发件人邮箱或授权码未配置，请在插件配置中填写后重载插件"))
            return

        result = await self.email_sender.send_email(to, subject, content, nickname)
        await event.send(MessageChain().message(result.get("msg", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_list")
    async def admin_scheduled_list(self, event: AstrMessageEvent):
        """管理员指令：列出定时指令（持久化）"""
        args = event.message_str.strip().split()
        include = len(args) > 1 and args[1].lower() == "true"
        result = await self.list_scheduled_commands(event, include)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_cancel")
    async def admin_scheduled_cancel(self, event: AstrMessageEvent):
        """管理员指令：取消定时指令"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_scheduled_cancel <任务ID>"))
            return
        task_id = args[1]
        result = await self.cancel_scheduled_command(event, task_id)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_delete")
    async def admin_scheduled_delete(self, event: AstrMessageEvent):
        """管理员指令：彻底删除定时指令"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_scheduled_delete <任务ID>"))
            return
        task_id = args[1]
        result = await self.delete_scheduled_command(event, task_id)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_search")
    async def admin_search(self, event: AstrMessageEvent):
        """管理员指令：搜索联系人（好友/群聊）"""
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_search <关键词> [类型]  类型可选：all/friend/group"))
            return
        keyword = args[1]
        search_type = args[2] if len(args) > 2 else "all"
        result = await self.search_contacts(event, keyword, search_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_list")
    async def admin_list(self, event: AstrMessageEvent):
        """管理员指令：列出联系人（好友/群聊）"""
        args = event.message_str.strip().split()
        contact_type = args[1] if len(args) > 1 else "all"
        limit = int(args[2]) if len(args) > 2 and args[2].isdigit() else 20
        result = await self.list_contacts(event, contact_type, limit)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    # ==================== 新增群管理管理员指令（使用群管理工具） ====================
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban_user")
    async def cmd_ban_user(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/ban_user <QQ号> <禁言分钟>"))
            return
        user_id = args[1]
        try:
            minutes = int(args[2])
        except:
            await event.send(MessageChain().message("禁言时长必须是数字（分钟）"))
            return
        result = await self.set_group_ban(event, user_id, minutes * 60)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("unban_user")
    async def cmd_unban_user(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/unban_user <QQ号>"))
            return
        user_id = args[1]
        result = await self.set_group_ban(event, user_id, 0)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("kick")
    async def cmd_kick(self, event: AiocqhttpMessageEvent):
        if not self.config.get("kick_enabled", True):
            await event.send(MessageChain().message("❌ 踢人功能已被禁用（kick_enabled=false）"))
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/kick <QQ号>"))
            return
        user_id = args[1]
        result = await self.set_group_kick(event, user_id)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("whole_ban")
    async def cmd_whole_ban(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/whole_ban <on/off>"))
            return
        enable = args[1].lower() == "on"
        result = await self.set_group_whole_ban(event, enable)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_card")
    async def cmd_set_card(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/set_card <QQ号> <新群昵称>（空字符串取消）"))
            return
        user_id = args[1]
        card = args[2]
        result = await self.set_group_card(event, user_id, card)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("send_notice")
    async def cmd_send_notice(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/send_notice <公告内容>"))
            return
        content = args[1]
        result = await self.send_group_notice(event, content)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("del_notice")
    async def cmd_del_notice(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/del_notice <公告ID>"))
            return
        notice_id = args[1]
        result = await self.delete_group_notice(event, notice_id)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("list_files")
    async def cmd_list_files(self, event: AiocqhttpMessageEvent):
        result = await self.list_group_files(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("delete_group_file")
    async def cmd_delete_group_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/delete_group_file <file_id>"))
            return
        file_id = args[1]
        result = await self.delete_group_file(event, file_id)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("group_members")
    async def cmd_group_members(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_members_info(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    # ==================== 提示词注入（状态 + 记忆 + 群角色） ====================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: Any, *args, **kwargs) -> None:
        try:
            inject_parts = []
            # 状态注入
            status_desc = self.status_manager.get_current_status_desc()
            inject_parts.append(f"[系统状态] {status_desc}")

            # 记忆注入
            if self.config.get("enabled", True) and event.get_platform_name() in ["aiocqhttp", "qq"] and self.config.get("memory_inject_enabled", True):
                user_id = event.get_sender_id()
                max_memories = self.config.get("max_inject_memories", 5)
                memories = await self.memory_manager.get_latest_memories_for_inject(user_id, max_memories)
                if memories:
                    memory_lines = [f"[用户历史记忆] 该用户({user_id})的重要信息："]
                    for i, m in enumerate(memories, 1):
                        tags = f"[{', '.join(m.get('tags', []))}]" if m.get('tags') else ""
                        content = m.get('content', '').replace('\n', ' ').replace('\r', '')
                        memory_lines.append(f"{i}. {content} {tags}")
                    inject_parts.append("\n".join(memory_lines))

            # 群角色注入
            if self.config.get("enabled", True) and event.get_platform_name() in ["aiocqhttp", "qq"] and self.config.get("inject_group_role_enabled", True):
                if not event.is_private_chat():
                    group_id = event.get_group_id()
                    if group_id:
                        user_id = event.get_sender_id()
                        role = await self._get_group_member_role(group_id, user_id)
                        if role != "unknown":
                            inject_parts.append(f"[当前群身份] 用户 {user_id} 在本群({group_id})的身份是：{role}")

            if inject_parts:
                inject_text = "\n".join(inject_parts)
                if hasattr(request, 'system_prompt') and request.system_prompt:
                    request.system_prompt += f"\n{inject_text}\n"
                elif hasattr(request, 'system_prompt'):
                    request.system_prompt = inject_text + "\n"
        except Exception as e:
            logger.error(f"[注入] 失败: {e}")