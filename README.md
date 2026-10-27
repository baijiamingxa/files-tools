# 文件工具箱 · 下载 / 压缩解压 / 转码 / 清理 / 重命名 / 空间分析 / 查重

[![CI](https://github.com/baijiamingxa/txt-encoder/actions/workflows/ci.yml/badge.svg)](https://github.com/baijiamingxa/txt-encoder/actions/workflows/ci.yml)

> A Windows GUI for downloads, archives, text encoding conversion, cleanup, batch renaming, folder size analysis and duplicate-file finding.
> Double-click `run_gui.bat` or `启动文件工具箱.bat`. The original command-line tools remain available.
> Core features use the Python standard library. On first launch, missing portable archive engines are downloaded and verified automatically.

直链下载、压缩解压、文本转码、C 盘清理、批量重命名、空间分析和重复文件共用一个窗口，七个页签可以独立使用，
也可以按“下载 → 解压 → 转码”衔接处理。仓库地址和原有转码命令行接口保持兼容。

| 页签 | 功能 |
| --- | --- |
| 直链下载 | HTTP/HTTPS 分段下载、进度和速度、暂停后续传、不支持分段时回退 |
| 压缩 / 解压 | ZIP、tar、tar.gz、tar.xz 压缩与解压，gz/bz2/xz 单文件解压，批量任务与中文文件名修复 |
| 文本转码 | 批量识别和转换 GBK、GB2312、Big5、UTF-8、UTF-16 等编码，预览、报告与原地备份 |
| C 盘清理 | 扫描预定义的临时与缓存目录，按勾选项目清理、跳过占用文件 |
| 批量重命名 | 旧名/新名预览，序号、替换、前后缀、扩展名与修改日期命名，冲突阻止、失败回退、撤销最近一批 |
| 空间分析 | 按逻辑大小列出文件和文件夹、大小排序和筛选、目录下钻、打开位置、选中后确认删除 |
| 重复文件 | 先按大小分组，再完整计算 SHA-256；建议保留最新副本，勾选删除且每组至少留一份 |

这些基础功能**只依赖 Python 标准库，无需 pip 安装**。首次启动会自检运行环境，7z、AES ZIP、
zst、RAR 所需扩展引擎不足时，自动从官方来源下载并校验便携组件；已有可用组件会直接复用。
右上角“运行环境…”可查看进度、取消和重试，详见[工具箱说明](docs/工具箱说明.md#首次启动运行环境自检)。

![批量重命名页](docs/rename.png)

---

## 一、最快上手

### 方式 1：图形界面（推荐）

双击 **`启动文件工具箱.bat`** 或 **`run_gui.bat`**，打开统一窗口：

* 下载：输入直链并开始下载。完成后点击“下载结果 → 解压”或“下载结果 → 转码”。
* 解压：添加压缩包，核对输出目录，点击“开始解压”；完成后可点击“解压结果 → 转码”。
  选中了任务时只传递所选成功结果；未选择时传递全部成功解压的目录。右上角“打包压缩…”用于创建压缩包。
* 转码：按下面的步骤操作。传递结果只添加输入，不会自动开始转换。
* C 盘清理：点“① 扫描”看每类垃圾和隐私痕迹有多大，勾选后“② 清理”。
  带“管理员”的项会弹 UAC 授权；被占用的文件自动跳过。详见 `docs/工具箱说明.md`。
* 批量重命名：添加文件或文件夹，组合规则，先点“① 扫描预览”核对旧名和新名，
  再点“② 确认并执行”。冲突行会标红并阻止整批执行；可“撤销上次”恢复最近一批原名。
* 空间分析：选择文件夹或磁盘，扫描后按大小定位大文件和目录。“分析所选文件夹”可继续深入，
  “在此查重”会打开重复文件页并扫描相应范围。
* 重复文件：扫描后绿色行建议保留最新一份。“勾选建议副本”只勾选当前显示的其他副本，
  点击删除前仍需确认；程序会再次校验保留副本和待处理副本，并阻止整组删除。

空间分析和查重支持导出完整 CSV，包括未显示的条目和无法访问的路径。
删除操作优先使用 Windows 回收站并保留系统确认；回收站清空前，可用空间不一定增加。

在 **“文本转码”页**：

1. 点“选择文件夹…”挑一个目录（也可以直接把文件夹拖到 `run_gui.bat` 上）；
2. “目标编码”选 UTF-8（默认就是）；
3. 点 **① 扫描预览** —— 工具会先告诉你每个文件现在是什么编码、哪些需要转换；
4. 在列表里点文件，右边“内容预览”能直接看解码结果对不对；
5. 确认没问题，点 **② 开始转换**。

默认是“输出到新目录”，原文件一个都不会动，结果放在源文件夹旁边的 `文件夹名_utf8` 目录里。

如果只需要原来的独立转码窗口，运行 `py txt_encoder_gui.py`。

### 免装 Python：打包成 exe

不想装 Python，可以打包一个单文件 exe：

```bat
py -m pip install pyinstaller    :: 只需装一次
py build_exe.py
```

得到 **`dist\文件工具箱.exe`**，拷到任何 Windows 电脑双击就能用，下载、解压、转码功能齐全，
设置保存在 exe 旁边（所在目录不可写时自动改存到 `%APPDATA%\文件工具箱`），
在窗口里安装右键菜单也会指向这个 exe。
首次运行可能弹出 SmartScreen 提示，点“更多信息 → 仍要运行”即可。

EXE、窗口和任务栏使用专属的蓝色工具箱图标。透明 PNG 和多尺寸 ICO 位于 `assets/`，
设计和重建方法见 [图标设计说明](assets/图标设计说明.md)。图标资源已打包进 EXE，单独复制 EXE 即可使用。

### 直接从文件右键打开

首次使用双击 **`安装右键菜单.bat`**，或在工具箱右上角“文件右键菜单”中选择“安装 / 更新右键菜单”。
安装后右键 TXT、ZIP、RAR、7z 等文件，选择 **“通过文件工具箱打开”**：
TXT 进入转码页，压缩包进入解压页，文件路径会自动填好，等待你点击扫描或解压。
同时会把“文件工具箱”加入相应文件的“打开方式”候选列表。

安装只针对当前用户，无需管理员权限，原默认打开程序保留。
Windows 11 可在“显示更多选项”中找到菜单；详见 [Microsoft 的右键菜单说明](https://blogs.windows.com/blog/2021/07/19/extending-the-context-menu-and-share-dialog-in-windows-11/)。
需要移除时双击 **`移除右键菜单.bat`**。移动工具箱目录后，重新安装一次即可更新路径。

### 方式 2：拖一下就转

把文件或文件夹拖到 **`quick_convert_to_utf8.bat`** 上 —— 直接转成 UTF-8（无 BOM），
原文件不动，结果放在旁边的 `_utf8` 目录，并在工具目录生成 `last_report.csv` 报告。

### 方式 3：命令行

```bat
:: 先看看这些文件都是什么编码（不改任何东西）
python txt_encoder_cli.py --detect "D:\稿件"

:: 预演一遍：告诉你会怎么转，但不写盘
python txt_encoder_cli.py "D:\稿件" --target utf-8 --dry-run

:: 真正转换：输出到 D:\稿件_utf8，原文件保留
python txt_encoder_cli.py "D:\稿件" --target utf-8 --report 报告.csv

:: 原地把老 GBK 文件改成 UTF-8（自动生成 .bak 备份）
python txt_encoder_cli.py "D:\稿件" --target utf-8 --mode inplace
```

---

## 二、能做什么

**自动识别源编码**，不用你事先知道文件是 GBK 还是 Big5：

| 识别依据 | 说明 |
| --- | --- |
| BOM 文件头 | UTF-8 / UTF-16 LE / UTF-16 BE / UTF-32 的 BOM，准确率最高 |
| 无 BOM 的 UTF-16 | 按 NUL 字节的分布判断字节序（LE / BE） |
| 严格 UTF-8 校验 | 能严格解通 UTF-8 的，基本就是 UTF-8 |
| 双字节结构分析 | GB 系（GB2312/GBK/GB18030）的尾字节几乎都在 0xA1-0xFE；Big5 约一半落在 0x40-0x7E，用这个区分简繁中文 |
| 字符分布打分 | 逐个候选编码试解，按“解出来像不像正常文本”打分，最高分当选 |

支持的编码：UTF-8 / UTF-8 BOM / UTF-16(LE/BE) / UTF-32 / GB2312 / GBK / GB18030 /
Big5 / Big5-HKSCS / Shift-JIS / EUC-KR / Windows-1252(ANSI) / Latin-1 / ASCII，
以及任何 Python 认识的编码名（比如 `cp936`、`utf-16-le`）。

**C 盘清理**：临时文件、浏览器缓存、缩略图、最近使用记录、回收站、Windows 更新缓存、
事件日志等一次看全。只清理位置固定、可再生成的系统目录，先扫描后清理，
绝不碰你的文档、密码和浏览器登录状态。

**批量转换**的三件事：

| 项目 | 可选做法 |
| --- | --- |
| 换行符 | 保持原样 / 统一 CRLF（Windows）/ 统一 LF（Unix）/ 统一 CR |
| BOM | 与原文件一致 / 不添加 / 添加 / 按编码惯例 |
| 输出方式 | 输出到新目录（默认，最安全）/ 原目录加后缀另存 / 原地覆盖（自动备份 .bak） |

---

## 三、为什么可以放心用

* **默认不改原文件**：输出到新目录，并保留原有的子目录结构。
* **大文件不占内存**：读写全部按 1 MB 分块（流式转码），内存占用与文件大小无关。
  实测把 256 MB 的 GBK 文件转成 UTF-8，进程内存峰值约 30 MB；编码检测也只抽取
  开头/中段/末段几处样本，不会把整个文件读进来。日志每个文件只有一条摘要，不打印内容。
* **先预演再执行**：`--dry-run` 或界面上的“① 扫描预览”，会算出每个文件会变成什么样，但不写盘。
* **原子写入**：先写临时文件再替换，断电或中断也不会留下半个文件。
* **能转就转，不能转就报错**：比如想把中文转成 ASCII，工具会明确告诉你“目标编码无法表示某个字符”，而不是塞一堆 `?` 进去；解码失败的文件原样保留。
* **自动跳过**：已经是目标编码的文件不会重复转换；二进制文件（图片、压缩包）会被识别出来跳过。
* **可核对**：转换结果可以导出 CSV 报告（界面上的“保存报告”），里面有每个文件的源编码、置信度、字节数变化。

> 唯一需要小心的是“原地覆盖”。界面选到这一项会有确认提示；命令行请保留默认的 `--no-backup` 不要加，也就是让 `.bak` 备份生效。

---

## 四、命令行参数速查

```
python txt_encoder_cli.py <路径...> [选项]

  -s, --source       源编码，默认 auto 自动检测
  -t, --target       目标编码，默认 utf-8
  -b, --bom          keep(默认) / none / add / default
  -n, --newline      keep(默认) / crlf / lf / cr
  -m, --mode         newdir(默认) / suffix / inplace
  -o, --out          输出目录（mode=newdir 时用）
  -e, --ext          处理的扩展名，如 txt,md,log；all 表示全部文件
  -r, --recursive    递归子目录（默认开）；--no-recursive 关闭
      --no-skip-same 已是目标编码也重新写一遍
      --no-backup    原地覆盖时不备份（危险，不推荐）
      --on-error     skip(默认) / replace：解码失败时跳过或用替代字符继续
      --dry-run      只预演不写盘
      --detect       只检测编码，不转换
      --jobs N       并发线程数（文件很多时可设 4~8 提速）
      --report 文件  把结果写成 CSV 报告
      --list-encodings  列出所有支持的编码名
```

---

## 五、目录里都是什么

| 文件 | 作用 |
| --- | --- |
| `run_gui.bat` | **双击启动图形界面** |
| `quick_convert_to_utf8.bat` | 把文件/文件夹拖上去，一键转 UTF-8 |
| `txt_encoder_gui.py` | 图形界面源码 |
| `txt_encoder_cli.py` | 命令行源码 |
| `txt_encoder_core.py` | 核心引擎（编码检测 + 转码），可被其它脚本 import |
| `make_sample_files.py` | 生成一批各种编码的示例文件，方便试手 |
| `build_exe.py` | 把工具箱打包成单文件 `dist\文件工具箱.exe`（免装 Python 直接运行） |
| `c_cleaner.py` | C 盘清理（临时文件/浏览器缓存/更新缓存等），含图形页签和管理员任务管道 |
| `c_cleaner_selftest.py` | C 盘清理核心自检（注入临时目录，不碰真实系统路径） |
| `selftest.py` | 功能自检（53 项断言，含分块边界用例） |
| `gui_smoketest.py` | 图形界面自动冒烟测试（自动创建窗口、跑完扫描+转换） |
| `.github/workflows/ci.yml` | 持续集成：Ubuntu / Windows × Python 3.9 / 3.12 / 3.13 上跑自检和命令行全流程 |
| `.gitignore` / `.gitattributes` | 忽略临时产物、统一换行符 |
| `LICENSE` | MIT 许可证 |

想试手的话，先让工具自己造一批样例：

```bat
python make_sample_files.py
```

它会在 `示例文件\` 目录里生成 12 个样例（GBK / GB2312 / GB18030 / Big5 / UTF-8 / UTF-8 BOM /
UTF-16 / 无 BOM 的 UTF-16 / 纯英文 / 子目录嵌套 / 一个二进制文件），
正好可以把本工具的各种识别分支都走一遍。这些演示文件不会提交到仓库里。

想把核心引擎用在别的脚本里：

```python
from txt_encoder_core import ConvertOptions, convert_file, detect_file

print(detect_file("老文件.txt").describe())      # 看看是什么编码

opts = ConvertOptions(target_encoding="utf-8", output_mode="newdir")
result = convert_file("老文件.txt", opts, root=".")   # 转成 UTF-8
print(result.summary())
```

---

## 六、环境要求与自检

* Python 3.9 以上（开发环境是 3.14），**无需 pip 安装任何东西**。
* 每次推送都会在 GitHub Actions 上自动跑一遍：Ubuntu + Windows 两个平台，Python 3.9 / 3.12 / 3.13
  三个版本，检查原有转码功能、工具箱下载与压缩集成流程、命令行转换并上传报告；Windows 还运行两套真实 GUI 测试。
* 想确认环境没问题，在项目目录运行：

```bat
python selftest.py        :: 全部通过 ✔ 说明检测和转码逻辑正常
python gui_smoketest.py   :: 全部通过 ✔ 说明图形界面正常
python toolbox_selftest.py       :: 下载、暂停续传、压缩解压和转码串联
python toolbox_gui_smoketest.py  :: 统一窗口内的完整操作流程
python windows_integration_selftest.py :: 右键菜单安装、更新和移除，使用隔离注册表分支
python batch_rename_selftest.py  :: 改名、冲突、撤销和中断回退，仅使用临时文件
python storage_selftest.py      :: 空间统计、查重、保留副本和回收前复查，仅使用临时样本
python c_cleaner_selftest.py     :: C 盘清理的扫描、清理和管理员任务管道
```

---

## 七、常见问题

**Q：转完之后 Excel / 记事本还是乱码？**
Excel 打开 CSV 默认按系统 ANSI 编码读。要么目标编码选 `GBK（Windows 记事本“ANSI”）`，
要么选 `UTF-8（带 BOM，Excel/旧版记事本友好）`，让 Excel 知道这是 UTF-8。

**Q：GBK、GB2312、GB18030 该选哪个？**
GB2312 是最老、字最少的国标；GBK 是它的超集（Windows 上的“ANSI”就是 GBK）；
GB18030 又是 GBK 的超集，能表示全部汉字。**不确定就选 GB18030**，
它能完整读回 GBK / GB2312 的内容。

**Q：识别出来的是 GB2312，但我记得文件是 GBK 存的？**
如果文件里的字全都能用 GB2312 表示，两者字节完全相同，工具会报最精确的那个名字，
对转换结果没有任何影响。

**Q：BOM 是什么，要不要加？**
BOM 是文件开头三个字节（EF BB BF），用来告诉程序“这是 UTF-8”。
中文环境下如果文件主要给程序、网站、Git 用，**不要加**；如果主要给 Excel 打开，建议加。
默认“与原文件一致”可以保持原有风格不变。

**Q：几百 MB 甚至几个 GB 的文件会不会吃满内存 / 把日志刷爆？**
不会。转码是“读一块 → 解码 → 编码 → 写一块”，任何时刻内存里只有一块（默认 1 MB）
数据，所以 5 KB 的小文件和 5 GB 的日志占用是同一个量级；实测 256 MB 的文件，
进程内存峰值约 30 MB。检测大文件时也只抽开头/中段/末段几处样本，
而且抽的是“以换行符开头结尾”的干净块，不会把多字节字符截断导致误判。
控制台和日志面板每个文件只输出一条摘要（文件名、编码、大小、状态），
从不打印文件内容，所以文件再大也不会把日志塞满。
块大小想调可以直接改 `txt_encoder_core.py` 里的 `CHUNK_SIZE`。

**Q：会不会把我的文件改坏？**
默认不会动原文件；转换失败的文件一律保持原样；原地覆盖会先备份 `.bak`。
另外强烈建议先用“扫描预览 / --dry-run”看一眼再执行。

---

## 八、许可证

本项目采用 MIT 许可证，见 [LICENSE]。
欢迎提 Issue、PR，或者把它改成你自己需要的样子。
