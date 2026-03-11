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
    # 插件名称
    plugin_name = "媒体库服务器刷新"
    # 插件描述
    plugin_desc = "入库后自动刷新Emby/Jellyfin/Plex服务器海报墙（已优化多剧集去重逻辑）。"
    # 插件图标
    plugin_icon = "refresh2.png"
    # 插件版本
    plugin_version = "1.3.4"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediaserverrefresh_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _delay = 0
    _mediaservers = None

    # 延迟相关的属性
    _in_delay = False
    _pending_items: List[RefreshMediaItem] = []
    _end_time = 0.0
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._delay = int(config.get("delay") or 0)
            self._mediaservers = config.get("mediaservers") or []

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器，请检查配置")
            return None

        services = MediaServerHelper().get_services(name_filters=self._mediaservers)
        if not services:
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
            else:
                active_services[service_name] = service_info

        return active_services if active_services else None

    def get_state(self) -> bool:
        return self._enabled

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
                                        'props': {'model': 'enabled', 'label': '启用插件'}
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
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in MediaServerHelper().get_configs().values()]
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
        ], {"enabled": False, "delay": 0}

    @eventmanager.register(EventType.TransferComplete)
    def refresh(self, event: Event):
        if not self._enabled:
            return

        event_info: dict = event.event_data
        if not event_info:
            return

        # 获取媒体服务器实例
        active_services = self.service_infos
        if not active_services:
            return

        # 解析转移信息
        transferinfo: TransferInfo = event_info.get("transferinfo")
        if not transferinfo or not transferinfo.target_diritem or not transferinfo.target_diritem.path:
            return

        # 1. 路径预处理：如果是文件，则获取其所属文件夹（去重核心）
        target_path = Path(transferinfo.target_diritem.path)
        refresh_path = target_path.parent if target_path.is_file() else target_path

        # 2. 构造刷新项目
        mediainfo: MediaInfo = event_info.get("mediainfo")
        item = RefreshMediaItem(
            title=mediainfo.title,
            year=mediainfo.year,
            type=mediainfo.type,
            category=mediainfo.category,
            target_path=refresh_path,
        )

        def debounce_delay(duration: int):
            """延迟防抖优化"""
            with self._lock:
                self._end_time = time.time() + float(duration)
                if self._in_delay:
                    return False
                self._in_delay = True

            while time.time() < self._end_time:
                time.sleep(1)

            with self._lock:
                self._in_delay = False
            return True

        # 3. 加入待刷新队列并去重
        with self._lock:
            # 检查队列中是否已存在相同路径
            if not any(str(x.target_path) == str(item.target_path) for x in self._pending_items):
                self._pending_items.append(item)
            else:
                logger.debug(f"路径 {refresh_path} 已在刷新队列中，跳过添加")

        # 4. 延迟逻辑处理
        if self._delay > 0:
            logger.info(f"项目 {item.title} 已加入队列，等待 {self._delay} 秒后统一刷新...")
            if not debounce_delay(self._delay):
                # 仍在延迟中，由第一个启动的线程负责后续执行
                return
            
            with self._lock:
                items_to_process = self._pending_items[:]
                self._pending_items = []
        else:
            items_to_process = [item]

        # 5. 分发刷新请求
        for name, service in active_services.items():
            instance = service.instance
            if hasattr(instance, 'refresh_library_by_items'):
                logger.info(f"[{name}] 开始刷新队列中的 {len(items_to_process)} 个项目...")
                for r_item in items_to_process:
                    try:
                        # 逐个下发，确保 Emby 能够正确排队处理
                        instance.refresh_library_by_items([r_item])
                        logger.info(f"[{name}] 成功下发刷新指令: {r_item.title} ({r_item.target_path})")
                        # 加入 1 秒间隔，保护服务器
                        time.sleep(1)
                    except Exception as e:
                        logger.error(f"[{name}] 刷新 {r_item.title} 失败: {str(e)}")
            elif hasattr(instance, 'refresh_root_library'):
                logger.info(f"[{name}] 不支持按项刷新，执行全量库刷新")
                instance.refresh_root_library()
            else:
                logger.warning(f"[{name}] 未找到可用的刷新接口")

    def stop_service(self):
        with self._lock:
            self._end_time = 0.0
