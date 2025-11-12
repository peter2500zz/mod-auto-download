from typing import Optional, Self
from rich.progress import Progress
import requests
import re
import json

from moderr import ModError, SlugNotValid, ModNotFoundError


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
        """
        请求Modrinth的API来初始化自身信息
        """
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
        """
        根据给定游戏版本和加载器来查找最新的模组
        """
        if not self.project:
            raise ModError(f"模组 {self.slug} 还未初始化")
        
        params = {
            "loaders": json.dumps([loader]),
            "game_versions": json.dumps([game_version]),
            # 因为有的模组总是在开发版本，所以常态开启了
            "featured": json.dumps(True),
        }

        result = requests.get(self.API + f"/project/{self.project.get("id")}/version", params=params)

        if result.status_code == 404:
            raise ModNotFoundError(f"模组 {self.project.get("title")} 没有适用于 Minecraft {game_version} {loader} 加载器的版本")
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
                    progress.print(f"找到版本: {self.project.get("title")} {version.get("version_number")}")
                self.current_version = version
                break

        else:
            raise ModNotFoundError(f"模组 {self.project.get("title")} 没有适用于 Minecraft {game_version} {loader} 加载器的版本")

