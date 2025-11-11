from typing import Optional, Self
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress
from rich.console import Console
from rich.tree import Tree
import requests
import re
import json
import sys


class ModError(Exception):
    pass

class SlugNotValid(ModError):
    def __init__(self, bad_slug: str) -> None:
        super().__init__(f"{bad_slug} 是无效的 mod slug")

class ModNotFoundError(ModError):
    pass


class Mod:
    API: str = "https://api.modrinth.com/v2"

    slug: str
    # 结构 https://docs.modrinth.com/api/operations/getproject/
    project: Optional[dict] = None

    def __init__(self, url: str) -> None:
        slug = url.split("/")[-1]

        # Modrinth 的 slug 必须匹配如下正则表达式
        if re.search(r"^[\w!@$()`.+,\"\-']{3,64}$", slug):
            self.slug = slug

        else:
            raise SlugNotValid(slug)

    def init(self, progress: Progress) -> Self:
        progress.print(f"解析: [bright_black]{self.slug}[/bright_black]")
        
        result = requests.get(self.API + f"/project/{self.slug}")

        if result.status_code == 404:
            raise ModNotFoundError(f"无法找到模组 {self.slug}")
        elif result.status_code != 200:
            result.raise_for_status()

        self.project = result.json()

        if self.project:
            progress.print(f"解析成功: [green]{self.project.get("title")}[/green]")

            return self

        raise ModError(f"{self.slug} 的数据解析失败")

    def query_version(self, version: str, loader: str):
        if not self.project:
            raise ModError(f"模组 {self.slug} 还未初始化")
        
        params = {
            "loaders": json.dumps([loader]),
            "game_versions": json.dumps([version]),
            "featured": True,
        }

        result = requests.get(self.API + f"/project/{self.project.get("id")}/version")

        if result.status_code == 404:
            raise ModNotFoundError(f"模组 {self.project.get("title")} 没有适用于 Minecraft {version} {loader} 加载器的版本")
        elif result.status_code != 200:
            result.raise_for_status()

        


class ModManager:
    # 维护一个线程池来并发请求
    __pool: ThreadPoolExecutor
    console: Console
    mods: list[Mod] = []
    __cached_mods: list[Mod] = []
    
    def __init__(self, threads: int = 4) -> None:
        self.__pool = ThreadPoolExecutor(threads)
        self.console = Console()
        self.mods = []
        # self.__cached_mods = []

    def init_mod(self):
        errors: list[ModError] = []

        with Progress() as progress:
            task_id = progress.add_task("解析模组", True, len(self.mods))

            futures = [self.__pool.submit(mod.init, progress) for mod in self.mods]

            for future in as_completed(futures):
                try:
                    future.result()
                except ModError as e:
                    progress.print(f"[red]错误: {e}[/red]")
                    errors.append(e)
                except Exception:
                    progress.stop()
                    self.__pool.shutdown()
                    self.console.line()
                    self.console.print_exception()
                    sys.exit(1)
                finally:
                    progress.update(task_id, advance=1)

        if errors:
            e_tree = Tree("由于以下原因，程序终止:")
            for error in errors:
                e_tree.add(f"[red]{error}[/red]")
            self.console.print(e_tree)
            sys.exit(1)

    def set_version(self, version: str, loader: str):
        errors: list[ModError] = []

        with Progress() as progress:
            task_id = progress.add_task("搜索版本", True, len(self.mods))

            futures = [self.__pool.submit(mod.init, progress) for mod in self.mods]

            for future in as_completed(futures):
                try:
                    future.result()
                except ModError as e:
                    progress.print(f"[red]错误: {e}[/red]")
                    errors.append(e)
                except Exception:
                    progress.stop()
                    self.__pool.shutdown()
                    self.console.line()
                    self.console.print_exception()
                    sys.exit(1)
                finally:
                    progress.update(task_id, advance=1)

        if errors:
            e_tree = Tree("由于以下原因，程序终止:")
            for error in errors:
                e_tree.add(f"[red]{error}[/red]")
            self.console.print(e_tree)
            sys.exit(1)

def main():
    mm = ModManager(10)

    mm.mods.extend([
        Mod("https://modrinth.com/mod/sodium"),
        Mod("https://modrinth.com/mod/reeses-sodium-options"),
    ])

    mm.init_mod()

if __name__ == "__main__":
    main()
