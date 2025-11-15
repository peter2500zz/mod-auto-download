from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from mod import Mod

class ModError(Exception):
    pass

class SlugNotValid(ModError):
    def __init__(self, bad_slug: str) -> None:
        super().__init__(f"{bad_slug} 是无效的 mod slug")

class ModNotFoundError(ModError):
    except_mod: Mod

    def __init__(self, msg: str, except_mod: Mod) -> None:
        self.except_mod = except_mod
        super().__init__(msg)

class ModIncompatibleError(ModError):
    def __init__(self, moda: Mod, modb: Mod) -> None:
        if not moda.project_data() or not modb.project_data():
            super().__init__(f"{moda.slug_or_id} 与 {modb.slug_or_id} 不兼容")
        else:
            super().__init__(f"{moda.title()} 与 {modb.title()} 不兼容")

