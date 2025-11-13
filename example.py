from rich.console import Console

from manager import ModManager
from mod import Mod


def get_mod(
    mods: list[Mod], 
    game_version: str, 
    loader: str, 
    download_dir: str, 
    for_client: bool = True, 
    for_server: bool = True, 
    allow_optional_mod: bool = False, 
    threads: int = 4
):
    console = Console()

    if not any([for_client, for_server]):
        console.print("既不要客户端也不要服务器，何意味？")
        return

    try:
        with ModManager(threads) as mm:
            mm.mods = mods

            if not mm.init_mod(for_client, for_server):
                return
            if not mm.set_version(game_version, loader):
                return
            if not mm.resolve_dependencies(allow_optional_mod):
                return
            if not mm.download_mods(download_dir):
                return

    except Exception:
        console.line()
        console.print_exception()


if __name__ == "__main__":
    mods = [
        Mod("hexal"),
        Mod("antique-atlas-4"),
        Mod("oculus"),
        Mod("embeddium"),
        Mod("https://modrinth.com/mod/rubidium-extra"),
        Mod("https://modrinth.com/mod/rubidium"),
        Mod("fastload"),
        Mod("textrues-embeddium-options"),
        Mod("https://modrinth.com/mod/goety-cataclysm"),
        Mod("https://modrinth.com/mod/textrues-rubidium-options")
    ]

    get_mod(
        # 模组列表
        mods,
        # Minecraft 版本
        "1.20.1",
        # 模组加载器
        "forge",
        # 模组下载目录
        "mods",
        # 下载可选依赖
        True,
        True,
        True,
        # 最大线程数
        10,
    )
