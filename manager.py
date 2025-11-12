from concurrent.futures import ThreadPoolExecutor, as_completed
import networkx as nx
from rich.progress import Progress
from rich.console import Console
from rich.tree import Tree
from pyvis.network import Network

from moderr import ModError
from mod import Mod


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

    def init_mod(self) -> bool:
        """
        统一初始化已有的模组

        return: 是否应该继续
        """

        errors: list[ModError] = []
        with Progress() as progress:
            task_id = progress.add_task("解析模组", True, len(self.mods))

            # 多线程初始化
            futures = [self.__pool.submit(mod.init, progress) for mod in self.mods]

            # 等待所有任务结束
            for future in as_completed(futures):
                try:
                    future.result()
                except ModError as e:
                    progress.print(f"[yellow]警告: {e}[/yellow]")
                    errors.append(e)
                except Exception:
                    # 真的很异常的异常应当上报
                    progress.stop()
                    self.__pool.shutdown()

                    raise
                finally:
                    progress.update(task_id, advance=1)

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.console.print(e_tree)
            return False
        return True

    def set_version(self, version: str, loader: str) -> bool:
        """
        设置目标游戏版本和加载器
        会同时查询所有模组在此版本下的可用性

        return: 是否应该继续
        """

        self.target_version = version
        self.target_loader = loader

        errors: list[ModError] = []
        with Progress() as progress:
            task_id = progress.add_task("搜索版本", True, len(self.mods))

            # 多线程检查版本可用性
            futures = [self.__pool.submit(mod.query_version, self.target_version, self.target_loader, progress) for mod in self.mods]

            # 等待所有任务结束
            for future in as_completed(futures):
                try:
                    future.result()
                except ModError as e:
                    progress.print(f"[yellow]警告: {e}[/yellow]")
                    errors.append(e)
                except Exception:
                    # 真的很异常的异常应当上报
                    progress.stop()
                    self.__pool.shutdown()

                    raise
                finally:
                    progress.update(task_id, advance=1)

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.console.print(e_tree)
            return False
        return True

    def resolve_dependencies(self, allow_optional_mod: bool = False) -> bool:
        """
        解析所有模组在设定版本下的依赖

        return: 是否应该继续
        """

        # 用内置类型先存储图的信息，方便进行修改
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

            # mod_cur 存储本次循环需要解析依赖的模组
            # 初始化为已有的模组
            mod_cur = self.mods.copy()

            # 直到没有模组的依赖需要解析，一直运行
            while mod_cur:
                # 初始化本轮次模组产生的依赖暂存列表
                dep_mods: list[Mod] = []

                # 遍历本轮次所有需要解析的模组
                for mod in mod_cur:
                    if mod.project is None:
                        raise ModError

                    if mod.current_version is None:
                        # 如果这个模组没有当前版本的信息
                        # 标记为无法获取合适版本的依赖模组
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
                        "version": mod.current_version["id"],
                        "href": f"https://modrinth.com/mod/{mod.project["slug"]}"
                    }

                    # 获取该模组的依赖
                    deps: list[dict] = mod.current_version["dependencies"]

                    for dep in deps:
                        # 模组是否已经存在
                        mod_already_exists: bool = dep["project_id"] in nodes
                        # 模组是否冲突
                        mod_incompatible: bool = dep["dependency_type"] == "incompatible"
                        # 模组是否可选
                        mod_optional: bool = not allow_optional_mod and dep["dependency_type"] == "optional"
                        # 如果模组冲突或者可选，但并不需要这个模组，那就直接跳过
                        if dep.get("project_id") is None or ((mod_incompatible or mod_optional) and not mod_already_exists):
                            continue

                        # 添加依赖箭头
                        edges.append((dep["project_id"], mod.project["id"], edge_style[dep["dependency_type"]]))
                        # 模组已经存在（解析过了），不需要进行解析
                        if mod_already_exists:
                            continue
                        # 节点将在这次循环的下半部分初始化
                        nodes[dep["project_id"]] = None
                        # 添加到需要解析的模组列表
                        dep_mods.append(Mod(dep["project_id"]))

                # 快速初始化模组（因为是依赖）
                def init(mod: Mod):
                    mod.init()
                    mod.query_version(self.target_version, self.target_loader)
                    if mod.project is None or mod.current_version is None:
                        raise ModError
                    progress.print(f"解析到 {mod.project.get("title")} {mod.current_version.get("version_number")}")

                # 多线程解析依赖
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

                        raise
                    finally:
                        progress.update(task_id, advance=1)

                # 应用到暂存列表
                mod_cur = dep_mods

            # 解析结束，关掉进度条
            progress.remove_task(task_id)

        # 创建有向图
        dependencies: nx.DiGraph[str] = nx.DiGraph()

        # 存入节点
        for nid, attrs in nodes.items():
            if attrs is None:
                raise ModError(f"模组 {nid} 没有属性")
            else:
                if any(mod for mod in self.mods if mod.project and nid == mod.project["id"]):
                    # 如果这个模组节点是一开始提供的模组，标为深色绿色
                    attrs["color"] = "green"
                dependencies.add_node(nid, **attrs)

        # 存入边
        for u, v, attrs in edges:
            dependencies.add_edge(u, v, **attrs)

            # 获取边两侧的节点
            mod = nodes.get(v)
            dep = nodes.get(u)
            if mod is None or dep is None:
                raise ModError(f"{u} 到 {v} 两侧的模组不存在")

            # 处理存在问题的模组
            match attrs.get("warn"):
                # 不兼容
                case "incompatible":
                    # 找到所有与此模组有关系的模组
                    results = [(_v, _attrs) for _u, _v, _attrs in edges if _u == u]

                    # 存放依赖此模组的模组
                    true_deps = []
                    # 遍历有关系的模组列表
                    for _v, _attrs in results:
                        dep_mode = nodes.get(_v)
                        if dep_mode is None:
                            raise ModError(f"{_v} 不存在")
                        # 如果存在 warn 字段，说明这不是一个正常依赖关系，而是冲突关系
                        if "warn" not in _attrs:
                            true_deps.append(f"[yellow]{dep_mode["_label"]}[/yellow]")

                    # 如果有任何正常依赖它的模组
                    if true_deps:
                        dep_tree = Tree(f"[yellow]{dep["_label"]} 不兼容 {mod["_label"]}，但以下模组需要它[/yellow]")
                        for true_dep in true_deps:
                            dep_tree.add(true_dep)
                    else:
                        dep_tree = f"[yellow]{dep["_label"]} 不兼容 {mod["_label"]}[/yellow]"
                    errors.append(ModError(dep_tree))

        # 创建可视化图
        net = Network(width="100%", notebook=False, directed=True, cdn_resources='local')
        net.from_nx(dependencies)

        net.write_html("dependencies.html", notebook=False, open_browser=False)
        self.console.print("依赖图已保存为 [bold]dependencies.html[/bold]")

        self.dependencies = dependencies

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

            return False
        return True

