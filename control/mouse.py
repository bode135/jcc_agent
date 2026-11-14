"""鼠标/拖拽入口 (兼容别名): 通用输入层已迁移至 control.input.

统一 API: click_left / click_right / drag / press_key / release_key / tap_key,
前台/后台由 default_config.BACKGROUND_INPUT 控制. 本模块仅保留旧名称转发,
现有调用方 (click_point / drag_point) 无需改动.
"""

from control.input import click_left as click_point
from control.input import drag as drag_point

__all__ = ["click_point", "drag_point"]
