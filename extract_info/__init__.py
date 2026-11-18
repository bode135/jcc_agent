"""对局信息提取: 从 1920×1080 截图中提取结构化信息, 存入数组/dict.

纯图色方案: 只依赖截图像素 (整图一次 OCR + regions 坐标切片 + 颜色/模板判定),
不读游戏进程内存、不注入、不改游戏文件。

当前实现范围 (extract_info/extractor.py), 每条记录均带 rect 位置方框
(x1, y1, x2, y2), 供后续自动化操作定位:
    players: 右侧玩家列表 [{"id": 玩家名, "hp": 血量, "rect": 行方框}, ...], 自上而下
    shop:    商店 5 卡槽   [{"name": 棋子名, "price": 价格, "rect": 槽位方框}, ...], 从左到右
    game:    全局信息      [{"key": ..., "value": ..., "rect": 区域方框}, ...]
             字段: stage 回合号 / round_time 回合剩余秒数 / level 等级 / xp 经验 /
             gold 金币 / xp_buy_cost 购买经验费用 / refresh_cost 刷新费用 /
             free_refresh 免费刷新次数

说明: 等级旁 "10/18" 为经验条 (已确认), 真实人口显示在棋盘右缘 "x人口" 药丸, 暂未提取.
棋盘/备战席棋子定位见 recognize/board.py (图色方案), 不在本包内.
区域切片解析复用 recognize/extract.py 的解析器 (整图一次 OCR, 各字段按 regions 坐标切片),
后续新增字段在本模块内扩展.
"""
from extract_info.extractor import extract

__all__ = ["extract"]
