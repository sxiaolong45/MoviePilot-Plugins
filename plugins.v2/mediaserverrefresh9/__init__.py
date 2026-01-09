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
    plugin_desc = "入库后自动刷新Emby/Jellyfin/Plex服务器海报墙（优化版）。"
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
    _pending_items = []
    _end_time = 0.0
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._delay = config.get("delay") or 0
            self._mediaservers = config.get("mediaservers") or []

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers:
            return None
        services = MediaServerHelper().get_services(name_filters=self._mediaservers)
        if not services:
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if not service_info.instance.is_inactive():
                active_services[service_name] = service_info
        return active_services

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

        # 校验服务器配置
        services = self.service_infos
        if not services:
            return

        # 获取入库路径
        transferinfo: TransferInfo = event_info.get("transferinfo")
        if not transferinfo or not transferinfo.target_diritem or not transferinfo.target_diritem.path:
            return

        mediainfo: MediaInfo = event_info.get("mediainfo")
        item = RefreshMediaItem(
            title=mediainfo.title,
            year=mediainfo.year,
            type=mediainfo.type,
            category=mediainfo.category,
            target_path=Path(transferinfo.target_diritem.path),
        )

        # --- 优化后的逻辑部分 ---

        def debounce_worker(duration: int):
            """防抖核心逻辑"""
            with self._lock:
                self._end_time = time.time() + float(duration)
                if self._in_delay:
                    return False
                self._in_delay = True

            while time.time() < self._end_time:
                time.sleep(1)

            with self._lock:
                # 获取并清空待处理队列
                raw_items = self._pending_items
                self._pending_items = []
                self._in_delay = False
            
            # 对路径进行去重处理，避免重复刷新
            unique_items = []
            seen_paths = set()
            for x in raw_items:
                path_str = str(x.target_path)
                if path_str not in seen_paths:
                    unique_items.append(x)
                    seen_paths.add(path_str)
            
            self._do_refresh(unique_items)
            return True

        if self._delay:
            logger.info(f"已加入队列，{self._delay} 秒后开始刷新媒体库...")
            with self._lock:
                self._pending_items.append(item)
            # 开启或重置倒计时线程
            threading.Thread(target=debounce_worker, args=(self._delay,)).start()
        else:
            self._do_refresh([item])

    def _do_refresh(self, items: List[RefreshMediaItem]):
        """执行实际的刷新操作"""
        if not items:
            return

        active_services = self.service_infos
        if not active_services:
            return

        for name, service in active_services.items():
            try:
                if hasattr(service.instance, 'refresh_library_by_items'):
                    logger.info(f"通知 {name} 刷新 {len(items)} 个路径...")
                    # 循环逐个调用，确保 Emby 接收到所有 Item
                    for i, refresh_item in enumerate(items):
                        logger.info(f"[{i+1}/{len(items)}] 正在刷新: {refresh_item.target_path}")
                        # 包装成单项列表提交，确保底层 API 能够正确解析
                        service.instance.refresh_library_by_items([refresh_item])
                        # 细微停顿，防止 API 并发过载
                        time.sleep(0.5)
                
                elif hasattr(service.instance, 'refresh_root_library'):
                    logger.info(f"{name} 不支持按项刷新，执行全库扫描")
                    service.instance.refresh_root_library()
                else:
                    logger.warning(f"{name} 接口不受支持")
            except Exception as e:
                logger.error(f"{name} 刷新过程发生异常: {str(e)}")

    def stop_service(self):
        with self._lock:
            self._end_time = 0.0
