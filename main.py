from typing import Optional, Self
from concurrent.futures import ThreadPoolExecutor, as_completed
import networkx as nx
from rich.progress import Progress
from rich.console import Console
from rich.tree import Tree
from pyvis.network import Network
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

class ModIncompatibleError(ModError):
    def __init__(self, moda: Mod, modb: Mod) -> None:
        if not moda.project or not modb.project:
            super().__init__(f"{moda.slug} 与 {modb.slug} 不兼容")
        else:
            super().__init__(f"{moda.project.get("title")} 与 {modb.project.get("title")} 不兼容")

class Mod:
    API: str = "https://api.modrinth.com/v2"

    slug: str
    # 参见 https://docs.modrinth.com/api/operations/getproject/
    project: Optional[dict]
    # 参见 https://docs.modrinth.com/api/operations/getprojectversions/
    current_version: Optional[dict]

    def __init__(self, url: str) -> None:
        slug = url.split("/")[-1]

        # Modrinth 的 slug 必须匹配如下正则表达式
        if re.search(r"^[\w!@$()`.+,\"\-']{3,64}$", slug):
            self.slug = slug
            self.project = None
            self.current_version = None

        else:
            raise SlugNotValid(slug)

    def init(self, progress: Optional[Progress] = None) -> Self:
        if progress:
            progress.print(f"解析: [bright_black]{self.slug}[/bright_black]")
        
        result = requests.get(self.API + f"/project/{self.slug}")

        if result.status_code == 404:
            raise ModNotFoundError(f"无法找到模组 {self.slug}")
        elif result.status_code != 200:
            result.raise_for_status()

        self.project = result.json()

        if self.project:
            if progress:
                progress.print(f"解析成功: [green]{self.project.get("title")}[/green]")

            return self

        raise ModError(f"{self.slug} 的数据解析失败")

    def query_version(self, game_version: str, loader: str, progress: Optional[Progress] = None):
        if not self.project:
            raise ModError(f"模组 {self.slug} 还未初始化")
        
        params = {
            "loaders": json.dumps([loader]),
            "game_versions": json.dumps([game_version]),
            "featured": json.dumps(True),
        }

        result = requests.get(self.API + f"/project/{self.project.get("id")}/version", params=params)

        if result.status_code == 404:
            raise ModNotFoundError(f"模组 {self.project.get("title")} 没有适用于 Minecraft {game_version} {loader} 加载器的版本")
        elif result.status_code != 200:
            result.raise_for_status()

        versions: list[dict] = result.json()

        for version in versions:
            version_condition = game_version in version.get("game_versions", [])
            loader_condition = loader in version.get("loaders", [])

            if version_condition and loader_condition:
                if progress:
                    progress.print(f"找到版本: {self.project.get("title")} {version.get("version_number")}")
                self.current_version = version
                break

        else:
            raise ModNotFoundError(f"模组 {self.project.get("title")} 没有适用于 Minecraft {game_version} {loader} 加载器的版本")


class ModManager:
    # 维护一个线程池来并发请求
    __pool: ThreadPoolExecutor
    console: Console
    mods: list[Mod] = []
    target_version: str
    target_loader: str
    dependencies: nx.DiGraph
    
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
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[red]{error}[/red]")
            self.console.print(e_tree)
            sys.exit(1)

    def set_version(self, version: str, loader: str):
        self.target_version = version
        self.target_loader = loader
        errors: list[ModError] = []

        with Progress() as progress:
            task_id = progress.add_task("搜索版本", True, len(self.mods))

            futures = [self.__pool.submit(mod.query_version, self.target_version, self.target_loader, progress) for mod in self.mods]

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
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[red]{error}[/red]")
            self.console.print(e_tree)
            sys.exit(1)

    def resolve_dependencies(self):
        nodes: dict[str, dict | None] = {}
        edges: list[tuple[str, str, dict]] = []

        edge_style = {
            "required": {
                "color": "lightgreen"
            },
            "incompatible": {
                "label": "❌", 
                "color": "red", 
                "font": {
                    "align": "middle"
                },
                "warn": "incompatible"
            },
            "optional": {
                "color": "lightgrey"
            },
            "embedded": {
                "color": "lightpurple",
                "warn": "embedded"
            }
        }

        errors: list[ModError] = []

        with Progress() as progress:
            task_id = progress.add_task("解析依赖", True, total=None)

            mod_cur = []
            for mod in self.mods:
                mod_cur.append(mod)

            while mod_cur:

                dep_mods: list[Mod] = []
                for mod in mod_cur:
                    if mod.project is None:
                        raise ModError

                    if mod.current_version is None:
                        nodes[mod.project["id"]] = {
                            "label": f"{mod.project["title"]} (无法获取)", 
                            "_label": f"{mod.project["title"]}", 
                            "color": "red", 
                            "href": f"https://modrinth.com/mod/{mod.project["slug"]}"
                        }
                        continue

                    # 把自己加入图节点
                    nodes[mod.project["id"]] = {
                        "label": mod.project["title"], 
                        "_label": mod.project["title"], 
                        "color": "lightgreen", 
                        "href": f"https://modrinth.com/mod/{mod.project["slug"]}"
                    }

                    deps: list[dict] = mod.current_version["dependencies"]

                    for dep in deps:
                        if dep.get("project_id") is None or (dep["dependency_type"] == "incompatible" and dep["project_id"] not in nodes):
                            continue

                        edges.append((dep["project_id"], mod.project["id"], edge_style[dep["dependency_type"]]))
                        if dep["project_id"] in nodes:
                            continue
                        # 节点将在这次循环的下半部分初始化
                        nodes[dep["project_id"]] = None
                        dep_mods.append(Mod(dep["project_id"]))

                def init(mod: Mod):
                    mod.init()
                    mod.query_version(self.target_version, self.target_loader)
                    if mod.project is None or mod.current_version is None:
                        raise ModError
                    progress.print(f"解析到 {mod.project.get("title")} {mod.current_version.get("version_number")}")
                futures = [self.__pool.submit(init, dep_mod) for dep_mod in dep_mods]

                for future in as_completed(futures):
                    try:
                        future.result()
                    except ModError as e:
                        progress.print(f"[yellow]警告: {e}[/yellow]")
                        errors.append(e)
                    except Exception:
                        progress.stop()
                        self.__pool.shutdown()
                        self.console.line()
                        self.console.print_exception()
                        sys.exit(1)
                    finally:
                        progress.update(task_id, advance=1)

                mod_cur = dep_mods

            progress.remove_task(task_id)

        

        dependencies: nx.DiGraph[str] = nx.DiGraph()

        for nid, attrs in nodes.items():
            if attrs is None:
                raise ModError
            else:
                if any(mod for mod in self.mods if mod.project and nid == mod.project["id"]):
                    attrs["color"] = "green"
                dependencies.add_node(nid, **attrs)

        for u, v, attrs in edges:
            dependencies.add_edge(u, v, **attrs)
            mod = nodes[v]
            dep = nodes[u]
            if mod is None or dep is None:
                raise ModError

            match attrs.get("warn"):
                case "incompatible":
                    results = [(_v, _attrs) for _u, _v, _attrs in edges if _u == u]
                    true_deps = []
                    for _v, _attrs in results:
                        dep_mode = nodes[_v]
                        if dep_mode is None:
                            raise ModError
                        if "warn" not in _attrs:
                            true_deps.append(f"[yellow]{dep_mode["_label"]}[/yellow]")
                    if true_deps:
                        dep_tree = Tree(f"[yellow]{dep["_label"]} 不兼容 {mod["_label"]}，但以下模组需要它[/yellow]")
                        for true_dep in true_deps:
                            dep_tree.add(true_dep)
                    else:
                        dep_tree = f"[yellow]{dep["_label"]} 不兼容 {mod["_label"]}[/yellow]"
                    errors.append(ModError(dep_tree))

        net = Network(height="100vh", width="100vw", notebook=False, directed=True, cdn_resources='local')
        net.from_nx(dependencies)

        net.write_html("dependencies.html", notebook=False, open_browser=False)
        self.console.print("依赖图已保存为 [bold]dependencies.html[/bold]")

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                for arg in error.args:
                    if isinstance(arg, Tree):
                        e_tree.add(arg)
                    else:
                        e_tree.add(f"[yellow]{arg}[/yellow]")
            self.console.print(e_tree)
            self.console.print("请参阅日志或依赖图")

def main():
    mm = ModManager(10)

    mm.mods.extend([
        # # Mod("https://modrinth.com/mod/sodium"),
        # # Mod("https://modrinth.com/mod/reeses-sodium-options"),
        # # Mod("https://modrinth.com/mod/sound-physics-remastered"),
        
        # Mod("create"),
        # Mod("hex-casting"),
        #     Mod("caelus"),
        #     Mod("inline"),
        Mod("hexal"),
        # Mod("sophisticated-backpacks"),
        # Mod("sophisticated-storage"),
        # Mod("biomes-o-plenty"),
        # # Mod("l_enders-cataclysm"),
        # Mod("enigmatic-legacy"),
        # Mod("enigmatic-addons"),
        Mod("antique-atlas-4"),
        # Mod("quark"),
        # Mod("entityculling"),
        # Mod("ferrite-core"),
        # Mod("immediatelyfast"),
        # Mod("modernfix"),
        # Mod("memoryleakfix"),
        # Mod("modelfix"),
        Mod("oculus"),
        Mod("embeddium"),
        Mod("https://modrinth.com/mod/rubidium-extra"),
        Mod("https://modrinth.com/mod/rubidium"),
        # Mod("badoptimizations"),
        # Mod("packet-fixer"),
        Mod("fastload"),
        # Mod("starlight-forge"),
        # Mod("asyncparticles"),
        # Mod("attributefix"),
        Mod("textrues-embeddium-options"),
        Mod("https://modrinth.com/mod/goety-cataclysm"),
        Mod("https://modrinth.com/mod/textrues-rubidium-options")
    ])


    mm.init_mod()
    mm.set_version("1.20.1", "forge")
    mm.resolve_dependencies()

if __name__ == "__main__":
    main()
