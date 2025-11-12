from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from mod import Mod

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

