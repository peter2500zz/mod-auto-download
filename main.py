
from concurrent.futures import ThreadPoolExecutor


class Mod:
    slug: str
    
    def __init__(self, url: str) -> None:
        self.slug = url.split("/")[-1]

class ModManager:
    # 维护一个线程池来并发请求
    pool: ThreadPoolExecutor
    mods: list[Mod] = []
    
    def __init__(self, threads: int = 4) -> None:
        self.pool = ThreadPoolExecutor(threads)
        self.mods = []

def main():
    mm = ModManager(10)

    mm.mods.extend([
        Mod("https://modrinth.com/mod/sodium"),
        Mod("https://modrinth.com/mod/reeses-sodium-options"),
    ])


if __name__ == "__main__":
    main()
