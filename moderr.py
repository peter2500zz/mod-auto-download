from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from mod import Mod

class ModError(Exception):
    pass

class SlugNotValid(ModError):
    def __init__(self, bad_slug: str) -> None:
        super().__init__(f"{bad_slug} 是无效的 mod slug")

class ModNotFoundError(ModError):
    except_mod_id: Optional[str]

    def __init__(self, msg: str, except_mod_id: Optional[str]) -> None:
        self.except_mod_id = except_mod_id
        super().__init__(msg)

class ModIncompatibleError(ModError):
    def __init__(self, moda: Mod, modb: Mod) -> None:
        if not moda.project or not modb.project:
            super().__init__(f"{moda.slug} 与 {modb.slug} 不兼容")
        else:
            super().__init__(f"{moda.project.get("title")} 与 {modb.project.get("title")} 不兼容")

