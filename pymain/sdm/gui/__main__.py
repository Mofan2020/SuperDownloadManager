"""GUI 入口的 python -m 入口。

打包成可执行文件后，PyInstaller 会以这个模块作为入口。
"""

from .main import main

if __name__ == "__main__":
    main()
