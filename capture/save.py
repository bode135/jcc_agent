"""截图保存: 默认保存到 default_config.CAPTURE_DIR, 避免污染示例图片目录 images/."""

from pathlib import Path

from PIL import Image

from config.config import default_config


def save_to_dir(image: Image.Image, image_name: str,
                parent_dir: str | Path | None = None) -> Path:
    """将截图保存到指定目录, 返回实际保存路径.

    Args:
        image: PIL Image 对象 (由 capture 截图得到).
        image_name: 文件名, 如 "ldplayer_window.png".
        parent_dir: 保存目录, 默认为 default_config.CAPTURE_DIR (已在 .gitignore 中忽略).
    """
    parent = Path(parent_dir) if parent_dir else default_config.CAPTURE_DIR
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / image_name
    image.save(path)
    return path
