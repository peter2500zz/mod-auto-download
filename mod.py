from typing import Optional, Self, TYPE_CHECKING, Literal
if TYPE_CHECKING:
    from manager import RateLimiter
from abc import ABC, abstractmethod
from rich.progress import Progress
import requests
import re
import json

from moderr import ModError, SlugNotValid, ModNotFoundError


class Mod:
    API: str = "https://api.modrinth.com/v2"

    slug_or_id: str
    # 参见 https://docs.modrinth.com/api/operations/getproject/
    __project: Optional[dict]
    # 参见 https://docs.modrinth.com/api/operations/getprojectversions/
    __current_version: Optional[dict]
    # 参见 https://docs.modrinth.com/api/operations/getversion/
    file_data: Optional[dict]

    __target_version: Optional[str]
    __target_loader: Optional[str]

    def __init__(self, url: str) -> None:
        slug_or_id = url.split("/")[-1]

        # Modrinth 的 slug 必须匹配如下正则表达式
        if re.search(r"^[\w!@$()`.+,\"\-']{3,64}$", slug_or_id):
            self.slug_or_id = slug_or_id
            self.__project = None
            self.__current_version = None
            self.file_data = None
            self.__target_version = None
            self.__target_loader = None

        else:
            raise SlugNotValid(slug_or_id)

    def init(self, game_version: str, loader: str, require_client: bool = True, require_server: bool = True, progress: Optional[Progress] = None, rl: Optional[RateLimiter] = None) -> Self:
        """
        请求Modrinth的API来初始化自身信息
        """

        self.__target_version = game_version
        self.__target_loader = loader

        if progress:
            progress.print(f"解析 [bright_black]{self.slug_or_id}[/bright_black]")

        if rl:
            rl.wait()
        result = requests.get(self.API + f"/project/{self.slug_or_id}")

        if result.status_code == 404:
            raise ModNotFoundError(f"无法找到模组 {self.slug_or_id}", self.__project.get("id") if self.__project else None)
        elif result.status_code != 200:
            result.raise_for_status()

        self.__project = result.json()

        if self.__project:
            if require_client:
                match self.__project.get("client_side"):
                    case "unsupported":
                        raise ModNotFoundError(f"模组 {self.__project.get("title")} 没有客户端版本", self.__project.get("id"))
                    case "unknown":
                        if progress:
                            progress.print(f"[yellow]警告 {f"模组 {self.__project.get("title")} 不确定在客户端是否可用"}[/yellow]")

            if require_server:
                match self.__project.get("server_side"):
                    case "unsupported":
                        raise ModNotFoundError(f"模组 {self.__project.get("title")} 没有服务器版本", self.__project.get("id"))
                    case "unknown":
                        if progress:
                            progress.print(f"[yellow]警告 {f"模组 {self.__project.get("title")} 不确定在服务器是否可用"}[/yellow]")
            
            if progress:
                progress.print(f"成功 [green]{self.__project.get("title")}[/green]")

            return self

        raise ModError(f"{self.slug_or_id} 的数据解析失败")

    def query_version(self, progress: Optional[Progress] = None, rl: Optional[RateLimiter] = None):
        """
        根据给定游戏版本和加载器来查找最新的模组
        """

        loader = self.target_loader()
        game_version = self.target_version()

        params = {
            "loaders": json.dumps([loader]),
            "game_versions": json.dumps([game_version]),
            # 因为有的模组总是在开发版本，所以常态开启了
            "featured": json.dumps(True),
        }

        if rl:
            rl.wait()
        result = requests.get(self.API + f"/project/{self.id()}/version", params=params)

        if result.status_code == 404:
            raise self.__not_found()
        elif result.status_code != 200:
            result.raise_for_status()

        versions: list[dict] = result.json()

        for version in versions:
            # 确认版本与加载器是否匹配
            version_condition = game_version in version.get("game_versions", [])
            loader_condition = loader in version.get("loaders", [])

            if version_condition and loader_condition:
                # 如果匹配直接跳出并存储版本信息
                if progress:
                    progress.print(f"找到 [bright_black]{self.title()} {version.get("version_number")}[/bright_black]")
                self.__current_version = version
                break

        else:
            raise self.__not_found()

    def get_version(self, progress: Optional[Progress] = None, rl: Optional[RateLimiter] = None):
        if not self.__project or not self.__current_version:
            self.__not_init()
        if progress:
            progress.print(f"获取 [bright_black]{self.title()} {self.__current_version.get("version_number")}[/bright_black]")

        if rl:
            rl.wait()
        result = requests.get(self.API + f"/version/{self.__current_version.get("id")}")

        if result.status_code == 404:
            raise ModNotFoundError(f"无法找到 {self.title()} {self.__current_version.get("version_number")} 版本的下载链接", self.id())
        elif result.status_code != 200:
            result.raise_for_status()

        result_data: dict = result.json()

        for file in result_data.get("files", []):
            self.file_data = file

            break
        else:
            raise ModNotFoundError(f"{self.title()} {self.__current_version.get("version_number")} 没有任何可用的下载链接", self.id())

    def target_version(self) -> str:
        if self.__target_version is None:
            self.__not_init()
        return self.__target_version

    def target_loader(self) -> str:
        if self.__target_loader is None:
            self.__not_init()
        return self.__target_loader

    def __not_init(self):
        raise ModError(f"模组 {self.slug_or_id} 还未初始化")

    def __not_found(self):
        raise ModNotFoundError(f"模组 {self.title()} 没有适用于 Minecraft {self.target_version()} {self.target_loader()} 加载器的版本", self.id())

    def project_data(self) -> dict:
        if self.__project is None:
            self.__not_init()
        return self.__project

    def id(self) -> str:
        return self.project_data()["id"]

    def slug(self) -> str:
        return self.project_data()["slug"]

    def title(self) -> str:
        return self.project_data()["title"]

    def version_data(self) -> dict:
        if self.__current_version is None:
            raise self.__not_found()
        return self.__current_version

    def version(self) -> str:
        return self.version_data()["version_number"]

    def dependencies(self) -> list[VerDep | ModDep]:
        return list(map(generate_dep, self.version_data()["dependencies"]))


class Dep(ABC):
    id: str
    dep_type: Literal["required", "optional", "incompatible", "embedded"]
    file_name: str

    require_client: bool
    require_server: bool
    target_version: str
    target_loader: str

    def __init__(self, id: str) -> None:
        self.id = id

    # @abstractmethod
    # def to_mod(self) -> Mod:
    #     pass

class ModDep(Dep):
    pass

class VerDep(Dep):
    pass


def generate_dep(data: dict) -> ModDep | VerDep:
    dep: VerDep | ModDep

    if id := data.get("version_id"):
        dep = VerDep(id)
    elif id := data.get("project_id"):
        dep = ModDep(id)
    else:
        raise ModError("依赖无效")

    dep.dep_type = data["dependency_type"]
    dep.file_name = data.get("file_name", "")

    return dep