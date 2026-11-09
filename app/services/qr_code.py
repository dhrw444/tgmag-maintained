"""生成 Telegram 扫码登录用的二维码 PNG。

qrcode 会连带拉起 15 个 PIL 模块，而扫码登录是低频路径（用户主动点"扫码登录"才走）。
所以这里的 import 放在函数内：进程启动时不为它付内存，第一次真正需要时才加载。
"""

from __future__ import annotations

import io


def login_qr_png(url: str) -> bytes:
    """把 Telegram 登录深链渲染成 PNG 字节。

    本地生成，不把登录链接发给任何第三方服务。
    """
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
