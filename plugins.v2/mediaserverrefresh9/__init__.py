import threading
import time
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, RefreshMediaItem, ServiceInfo
from app.schemas.types import EventType


class MediaServerRefresh(_PluginBase):
    plugin_name = "媒体库服务器刷新"
    plugin_desc = "入库后自动刷新 Emby / Jellyfin / Plex 媒体库（不刷新根目录）"
    plugin_icon = "refresh2.png"
    plugin_version = "1.4.0"
    plugin_author = "jxxghp / customized"
    author_url = "https://github.com/jxxghp"
    plugin_config_prefix = "mediaserverrefresh_"
    plugin_order = 14
    auth_level = 1

    _enabled = False
    _delay = 0
    _mediaservers = None

    # 延迟防抖
    _in_delay = False
    _pending_items: List[RefreshMediaItem] = []
    _end_time = 0.0
    _lock = threading.Lock()

    # =========================
    # 初始化
    # =========================
    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._delay = config.get("delay", 0) or 0
            self._mediaservers = config.get("mediaservers") or []

    # =========================
    # 服务信息
    # =========================
    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器")
            return None

        services = MediaServerHelper().get_services(
            name_filters=self._mediaservers
        )
        if not services:
            return None

        active = {}
        for name, info in services.items():
            if info.instance.is_inactive():
                logger.warning(f"媒体服务器 {name} 未连接")
            else:
                active[name] = info
        return active or None

    def get_state(self) -> bool:
        return self._enabled

    # =========================
    # UI 表单
    # =========================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [
                                                {"title": c.name, "value": c.name}
                                                for c in MediaServerHelper().get_configs().values()
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay',
                                            'label': '延迟时间（秒）',
                                            'placeholder': '0'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "delay": 0,
            "mediaservers": []
        }

    # =========================
    # 插件页面（立即刷新按钮）
    # =========================
    def get_page(self) -> List[dict]:
        return [
            {
                "component": "VCard",
                "content": [
                    {"component": "VCardTitle", "content": "媒体库刷新"},
                    {
                        "component": "VCardActions",
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "flat",
                                    "onClick": {
                                        "api": "mediaserverrefresh/refresh_now"
                                    }
                                },
                                "content": "立即刷新（待处理项目）"
                            }
                        ]
                    }
                ]
            }
        ]

    # =========================
    # API
    # =========================
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/mediaserverrefresh/refresh_now",
                "method": "POST",
                "summary": "立即刷新媒体库（不刷新根目录）",
                "handler": self.refresh_now
            }
        ]

    # =========================
    # 手动刷新
    # =========================
    def refresh_now(self):
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        services = self.service_infos
        if not services:
            return {"success": False, "message": "没有可用的媒体服务器"}

        with self._lock:
            items = self._pending_items[:]
            self._pending_items.clear()

        if not items:
            return {"success": False, "message": "当前没有待刷新项目"}

        self._do_refresh(items)
        return {"success": True, "message": f"已刷新 {len(items)} 个项目"}

    # =========================
    # 事件触发刷新
    # =========================
    @eventmanager.register(EventType.TransferComplete)
    def refresh(self, event: Event):
        if not self._enabled:
            return

        info: dict = event.event_data or {}
        transfer: TransferInfo = info.get("transferinfo")
        mediainfo: MediaInfo = info.get("mediainfo")

        if not transfer or not mediainfo or not transfer.target_diritem:
            return

        item = RefreshMediaItem(
            title=mediainfo.title,
            year=mediainfo.year,
            type=mediainfo.type,
            category=mediainfo.category,
            target_path=Path(transfer.target_diritem.path)
        )

        with self._lock:
            self._pending_items.append(item)

        if not self._delay:
            self._flush_with_items()
            return

        self._debounce(self._delay)

    # =========================
    # 防抖
    # =========================
    def _debounce(self, delay: int):
        with self._lock:
            self._end_time = time.time() + delay
            if self._in_delay:
                return
            self._in_delay = True

        def waiter():
            while time.time() < self._end_time:
                time.sleep(1)
            self._flush_with_items()
            with self._lock:
                self._in_delay = False

        threading.Thread(target=waiter, daemon=True).start()

    def _flush_with_items(self):
        with self._lock:
            items = self._pending_items[:]
            self._pending_items.clear()

        if items:
            self._do_refresh(items)

    # =========================
    # 实际刷新逻辑（关键）
    # =========================
    def _do_refresh(self, items: List[RefreshMediaItem]):
        services = self.service_infos
        if not services:
            return

        # 目录级去重
        uniq = {}
        for i in items:
            uniq[str(i.target_path.parent)] = i
        items = list(uniq.values())

        for name, service in services.items():
            if hasattr(service.instance, "refresh_library_by_items"):
                # ⚠️ 逐个调用，绕开 Emby return 缺陷
                for item in items:
                    try:
                        service.instance.refresh_library_by_items([item])
                        time.sleep(0.2)
                    except Exception as e:
                        logger.error(f"{name} 刷新失败: {e}")
            else:
                logger.warning(f"{name} 不支持刷新")

    # =========================
    # 停止插件
    # =========================
    def stop_service(self):
        with self._lock:
            self._end_time = 0
            self._pending_items.clear()
