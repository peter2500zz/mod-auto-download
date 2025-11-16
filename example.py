from rich.console import Console
import sys

from manager import ModManager, ProgressGen
from mod import Mod


def exhaust(progress: ProgressGen) -> bool:
    while True:
        try:
            advance, total, message = next(progress)
            # print(f"{message} {advance}/{total}")
        except StopIteration as e:
            return e.value

def get_mod(
    mods: list[Mod], 
    game_version: str, 
    loader: str, 
    download_dir: str, 
    require_client: bool = False, 
    require_server: bool = False, 
    allow_optional_mod: bool = False, 
    threads: int = 4,
    console: Console = Console()
):
    try:
        with ModManager(threads, console) as mm:
            mm.mods = mods

            if exhaust(mm.init_mod(game_version, loader, require_client, require_server)):
                return
            if exhaust(mm.check_version()):
                return
            if exhaust(mm.resolve_dependencies(allow_optional_mod)):
                return
            if exhaust(mm.get_download_link()):
                return
            if exhaust(mm.download_mods(download_dir)):
                return

    except KeyboardInterrupt:
        mm.finish()
        sys.exit(1)
    except Exception:
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
    )
