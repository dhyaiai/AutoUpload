"""
文件合并模块
功能: 将试题文件和答案文件合并为一个文件（试题在前，答案在后）
支持: .docx 纯 Python 合并（zip+XML，不启动 Word/WPS，绝对稳定）
      .doc  使用 Word 原生 COM 调用（Microsoft Word / WPS）
      .pdf  使用 pypdf 库合并
"""
import os
import time
import subprocess


class FileMerger:
    """文件合并器 — 静态方法集合，无需实例化"""

    SUPPORTED_EXTENSIONS = ('.doc', '.docx', '.pdf')

    # Word COM 常量
    WD_STORY = 6          # wdStory — 文档全文范围
    WD_PAGE_BREAK = 7     # wdPageBreak — 分页符
    WD_COLLAPSE_END = 0   # wdCollapseEnd

    # Word/WPS ProgID 优先级: MS Word → WPS 个人版 → WPS 专业版
    # (WPS 会抢注 Word.Application, 所以第一项实际常解析到 WPS)
    PROG_IDS = ('Word.Application', 'WPS.Application', 'KWPS.Application')

    # ============ .docx 纯 Python 合并的 OOXML 命名空间 ============
    _NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    _NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    _NS_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
    _NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
    # 文档级配置关系类型: 合并时跳过（正文与媒体才复制）
    _SKIP_REL_TYPES = (
        'styles', 'settings', 'theme', 'fontTable', 'numbering',
        'webSettings', 'footnotes', 'endnotes', 'comments', 'glossaryDocument',
    )
    _IMG_CONTENT_TYPES = {
        'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg',
        'gif': 'image/gif', 'bmp': 'image/bmp', 'tiff': 'image/tiff',
        'emf': 'image/x-emf', 'wmf': 'image/x-wmf', 'svg': 'image/svg+xml',
        'ico': 'image/x-icon', 'webp': 'image/webp',
    }

    # =============== WPS 稳定性处理 ===============
    # 实测结论(2026-08-14, WPS 12.1.0.28043):
    #   1. WPS 是单实例常驻架构, 但自动化服务器在反复操作后会退化:
    #      Documents.Add 抛 AttributeError/<unknown>.Add, 随后整个实例死亡
    #   2. 复用的 COM 实例在 PyInstaller 打包环境下"第二次 Add"必失败
    #   3. WPS 进程全灭后 Dispatch 能启动全新健康实例 → 一切正常
    # 因此采用: 每次合并使用新实例(不复用) + 合并后 Quit +
    #           Quit 后等待进程退出 + 失败时清理无窗口 WPS 进程后重试
    _QUIT_WAIT_TIMEOUT = 8.0   # Quit 后等待自动化实例进程消失的超时(秒)
    _RETRY_BACKOFF = 3.0       # 失败后等待 WPS 恢复/重建的时间(秒)
    _MAX_RETRY = 3             # 合并最大尝试次数

    @staticmethod
    def get_format(file_path: str) -> str:
        """获取文件扩展名（小写），如 '.docx'"""
        return os.path.splitext(file_path)[1].lower()

    @classmethod
    def merge(cls, question_path: str, answer_path: str, output_path: str) -> bool:
        """
        合并试题和答案文件，自动检测格式并调用对应方法。

        Args:
            question_path: 试题文件路径
            answer_path:   答案文件路径
            output_path:   合并后的输出文件路径

        Returns:
            True 表示合并成功

        Raises:
            ValueError: 格式不支持或不一致
            RuntimeError: 合并过程出错
        """
        q_fmt = cls.get_format(question_path)
        a_fmt = cls.get_format(answer_path)

        if q_fmt not in cls.SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件格式: {q_fmt}，仅支持 {', '.join(cls.SUPPORTED_EXTENSIONS)}")
        if q_fmt != a_fmt:
            raise ValueError(f"试题和答案文件格式不一致: {q_fmt} vs {a_fmt}")

        if q_fmt == '.docx':
            # .docx 是 zip+XML, 纯 Python 合并即可, 不依赖 Word/WPS/COM
            # (WPS 自动化服务器不稳定, 打包 exe 中第二次 COM 合并必失败)
            return cls._merge_docx_native(question_path, answer_path, output_path)
        elif q_fmt == '.doc':
            # .doc 是 OLE2 二进制格式, 必须走 Word COM
            return cls._merge_word(question_path, answer_path, output_path)
        elif q_fmt == '.pdf':
            return cls._merge_pdf(question_path, answer_path, output_path)

    # ==================== Word COM 合并 ====================

    @staticmethod
    def _wps_pids() -> set:
        """当前所有 wps.exe 进程 PID 集合"""
        try:
            out = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq wps.exe', '/FO', 'CSV'],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return set()
        pids = set()
        for line in out.strip().splitlines()[1:]:
            parts = line.strip('"').split('","')
            if len(parts) > 1 and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids

    @staticmethod
    def _visible_wps_pids() -> set:
        """
        有可见主窗口的 wps.exe PID 集合（用户正在使用的实例）。

        用 EnumWindows 枚举顶层窗口判断，有窗口的实例绝不清理
        （可能正打开着用户文档，杀掉会丢数据）。
        """
        try:
            import win32gui
            import win32process
        except ImportError:
            return set()
        pids = set()
        try:
            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    pids.add(pid)
                return True
            win32gui.EnumWindows(_cb, None)
        except Exception:
            return set()
        return pids

    @classmethod
    def _cleanup_wps(cls, kill: bool = False) -> None:
        """
        清理 WPS 自动化实例：
        1. 所有 wps.exe 进程列表中剔除有可见窗口的用户实例
        2. kill=True 时强制结束无窗口的 wps.exe（自动化后台实例，安全）
        """
        protected = cls._visible_wps_pids()
        for pid in cls._wps_pids():
            if pid in protected:
                continue
            if kill:
                try:
                    subprocess.run(
                        ['taskkill', '/PID', str(pid), '/F'],
                        capture_output=True, timeout=5)
                except Exception:
                    pass

    @classmethod
    def _wait_wps_exit(cls, old_pids: set, timeout: float) -> bool:
        """等待旧 WPS 自动化实例进程退出（超时返回 False）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            now = cls._wps_pids()
            if not (old_pids & now):   # 旧实例的进程全部消失
                return True
            time.sleep(0.3)
        return False

    @classmethod
    def _dispatch_word(cls, win32):
        """
        启动一个新的 Word/WPS COM 实例（不复用）。

        优先 DispatchEx 强制创建全新进程实例: 普通 Dispatch 会复用 WPS 常驻/
        已退化的实例(COM 单实例规则), 而退化实例的 Documents.Add 必失败。
        绝不 GetObject（会连接用户正在使用的实例, 干扰用户窗口）。
        """
        for pid in cls.PROG_IDS:
            try:
                word = win32.DispatchEx(pid)
            except Exception:
                try:
                    word = win32.Dispatch(pid)
                except Exception:
                    continue
            try:
                word.DisplayAlerts = 0     # 抑制保存/覆盖等弹窗
                word.Visible = False       # 后台静默合并
            except Exception:
                pass
            return word
        return None

    @classmethod
    def _merge_word(cls, question_path: str, answer_path: str, output_path: str) -> bool:
        """
        使用 Word COM 合并 .doc / .docx 文件。
        自动探测 Microsoft Word 和 WPS，每次使用新实例，用完退出。

        合并策略：新建空白文档 → 插入试题内容 →
        插入分页符 → 插入答案内容 → 另存为 → 关闭文档。

        WPS 稳定性策略：
        - 每次合并新实例（复用实例在打包环境第二次 Add 必失败）
        - 合并后 Quit 并等待进程退出（避免"僵尸实例"毒化下次合并）
        - 失败时清理无窗口 WPS 进程后重试（WPS 自动化服务器退化自愈）
        """
        try:
            import win32com.client as win32
        except ImportError:
            raise ImportError("需要安装 pywin32，请运行: pip install pywin32")

        last_error = None
        for attempt in range(1, cls._MAX_RETRY + 1):
            # 记录合并前的 wps 进程，用于 Quit 后等待退出
            before_pids = cls._wps_pids()

            word = cls._dispatch_word(win32)
            if word is None:
                last_error = RuntimeError(
                    "未找到可用的 Word 或 WPS 程序。\n"
                    "请确认已安装 Microsoft Word 或 WPS Office 文字组件。"
                )
                # Dispatch 失败可能是上次残留的僵尸实例占用 → 清理后重试
                cls._cleanup_wps(kill=True)
                time.sleep(cls._RETRY_BACKOFF)
                continue

            try:
                doc = word.Documents.Add()
                selection = word.Selection

                # 插入试题内容
                selection.EndKey(Unit=cls.WD_STORY)
                selection.InsertFile(question_path)

                # 插入分页符
                selection.InsertBreak(Type=cls.WD_PAGE_BREAK)

                # 插入答案内容
                selection.InsertFile(answer_path)

                # 根据扩展名选择保存格式
                ext = cls.get_format(output_path)
                if ext == '.doc':
                    file_format = 0   # wdFormatDocument（旧格式）
                else:
                    file_format = 16  # wdFormatDocumentDefault（.docx）

                doc.SaveAs(output_path, FileFormat=file_format)
                doc.Close(SaveChanges=0)
                return True

            except Exception as e:
                last_error = e
                try:
                    word.Quit()
                except Exception:
                    pass

            finally:
                # 等待本次启动的自动化实例进程退出（防僵尸）
                cls._wait_wps_exit(before_pids, cls._QUIT_WAIT_TIMEOUT)

            # 失败路径: 清理无窗口 WPS 进程（自动化实例退化/卡死自愈）
            cls._cleanup_wps(kill=True)
            time.sleep(cls._RETRY_BACKOFF)

        raise RuntimeError(f"Word 文档合并失败: {last_error}")

    # ==================== PDF 合并 ====================

    @classmethod
    def _merge_pdf(cls, question_path: str, answer_path: str, output_path: str) -> bool:
        """
        合并 PDF 文件，试题页在前、答案页在后。
        """
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            raise ImportError("需要安装 pypdf，请运行: pip install pypdf")

        try:
            writer = PdfWriter()

            for path in (question_path, answer_path):
                reader = PdfReader(path)
                for page in reader.pages:
                    writer.add_page(page)

            with open(output_path, 'wb') as f:
                writer.write(f)

            return True

        except Exception as e:
            raise RuntimeError(f"PDF 合并失败: {e}") from e

    # ==================== 纯 Python .docx 合并 ====================

    @staticmethod
    def _register_ns_prefixes(root) -> None:
        """注册 OOXML 常见命名空间前缀, 避免 ET 序列化时产生 ns0 污染。"""
        import xml.etree.ElementTree as ET
        common = {
            'w': FileMerger._NS_W,
            'r': FileMerger._NS_R,
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
            'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
            'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
            'wps': 'http://schemas.microsoft.com/office/word/2010/wordprocessingShape',
            'wpg': 'http://schemas.microsoft.com/office/word/2010/wordprocessingGroup',
            'w14': 'http://schemas.microsoft.com/office/word/2010/wordml',
            'w15': 'http://schemas.microsoft.com/office/word/2012/wordml',
            'm': 'http://schemas.openxmlformats.org/officeDocument/2006/math',
            'v': 'urn:schemas-microsoft-com:vml',
            'o': 'urn:schemas-microsoft-com:office:office',
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
            None: FileMerger._NS_PKG,   # rels 文件的默认命名空间
        }
        # 优先使用文档根元素上实际声明的前缀
        for attr, value in root.attrib.items():
            if attr.startswith('xmlns'):
                prefix = attr[6:] or None
                if prefix is not None:
                    common[prefix] = value
        for prefix, uri in common.items():
            try:
                ET.register_namespace(prefix, uri)
            except TypeError:
                pass

    @staticmethod
    def _serialize_xml(root) -> bytes:
        """序列化 XML 树为带声明和命名空间的字节串。"""
        import xml.etree.ElementTree as ET
        FileMerger._register_ns_prefixes(root)
        return ET.tostring(root, encoding='UTF-8', xml_declaration=True)

    @classmethod
    def _merge_docx_native(cls, question_path: str, answer_path: str, output_path: str) -> bool:
        """
        纯 Python 合并两个 .docx（zip + XML 级），不依赖 Word/WPS/COM。

        .docx 本质是 zip 包: 合并 word/document.xml 正文 → 试题内容 +
        分页符 + 答案内容。图片/嵌入对象等媒体关系一并迁移,
        自动处理 rId 冲突重映射与 [Content_Types] 补充。
        样式表不合并(答案的自定义样式缺失时 Word 自动回退默认样式)。

        从 2026-08 起 .docx 合并不再启动 Word/WPS:
        消除"合并第二个作业时弹黑窗/失败"问题(WPS 个人版自动化不稳定)。
        """
        import zipfile
        import copy
        from xml.etree import ElementTree as ET

        W, R = cls._NS_W, cls._NS_R

        def qt(tag):
            return f'{{{W}}}{tag}'

        def read_zip(path):
            with zipfile.ZipFile(path) as z:
                return {i.filename: z.read(i.filename) for i in z.infolist()}

        try:
            q_files = read_zip(question_path)
            a_files = read_zip(answer_path)

            if 'word/document.xml' not in q_files or 'word/document.xml' not in a_files:
                raise ValueError("docx 结构异常: 缺少 word/document.xml")

            q_doc = ET.fromstring(q_files['word/document.xml'])
            a_doc = ET.fromstring(a_files['word/document.xml'])
            q_body = q_doc.find(qt('body'))
            a_body = a_doc.find(qt('body'))
            if q_body is None or a_body is None:
                raise ValueError("docx 结构异常: 缺少 body")

            # 1) 答案的 sectPr(页面设置) 不合并, 沿用试题的页面设置
            for sect in list(a_body.findall(qt('sectPr'))):
                a_body.remove(sect)

            # 2) 媒体关系迁移: 收集答案正文引用的 rId
            rels_path = 'word/_rels/document.xml.rels'
            a_rels = {}
            if rels_path in a_files:
                rels_root = ET.fromstring(a_files[rels_path])
                for rel in rels_root.findall(f'{{{cls._NS_PKG}}}Relationship'):
                    a_rels[rel.get('Id')] = rel

            used_ids = set()
            for el in a_doc.iter():
                for name in (f'{{{R}}}embed', f'{{{R}}}id', f'{{{R}}}link'):
                    v = el.get(name)
                    if v:
                        used_ids.add(v)

            # q 已用 rId 集合 → 新 rId 从 max 起编号
            q_rels_root = None
            q_used_ids = set()
            if rels_path in q_files:
                q_rels_root = ET.fromstring(q_files[rels_path])
                for rel in q_rels_root.findall(f'{{{cls._NS_PKG}}}Relationship'):
                    q_used_ids.add(rel.get('Id'))
            max_num = 0
            for rid in q_used_ids:
                if rid.startswith('rId'):
                    try:
                        max_num = max(max_num, int(rid[3:]))
                    except ValueError:
                        pass

            id_map = {}          # 答案旧 rId → 新 rId
            copied_files = {}    # 答案 zip 内路径 → 输出 zip 内路径
            new_rels = []        # (new_id, type, target, mode)

            for rid in used_ids:
                rel = a_rels.get(rid)
                if rel is None:
                    continue
                rtype = rel.get('Type') or ''
                rtarget = rel.get('Target') or ''
                mode = rel.get('TargetMode')

                # 跳过文档级配置关系(styles/settings/theme/...)与页眉页脚
                if any(rtype.rstrip('/').endswith('/' + t) for t in cls._SKIP_REL_TYPES):
                    continue
                if rtype.rstrip('/').endswith(('/header', '/footer')):
                    continue

                new_id = f'rId{max_num + 1}'
                max_num += 1
                id_map[rid] = new_id

                if mode == 'External':   # 外部链接: 只追加关系不复制文件
                    new_rels.append((new_id, rtype, rtarget, 'External'))
                    continue

                # 解析 target → zip 内路径
                if rtarget.startswith('/'):
                    src = rtarget[1:]
                elif rtarget.startswith('http://') or rtarget.startswith('https://'):
                    continue
                else:
                    src = f'word/{rtarget}'

                if src not in a_files:
                    new_rels.append((new_id, rtype, rtarget, None))
                    continue

                # 与 q 已有文件重名 → 改名
                out_name = src
                if src in q_files or src in copied_files.values():
                    base, ext = os.path.splitext(src)
                    n = 2
                    while f'{base}_{n}{ext}' in q_files:
                        n += 1
                    out_name = f'{base}_{n}{ext}'
                copied_files[src] = out_name
                rel_target = out_name[5:] if out_name.startswith('word/') else out_name
                new_rels.append((new_id, rtype, rel_target, None))

            # 重写答案正文中的 rId 引用为新编号
            for el in a_doc.iter():
                for name in (f'{{{R}}}embed', f'{{{R}}}id', f'{{{R}}}link'):
                    v = el.get(name)
                    if v in id_map:
                        el.set(name, id_map[v])

            # 3) 组装正文: 试题内容 + 分页符 + 答案内容
            page_break_p = ET.Element(qt('p'))
            run = ET.SubElement(page_break_p, qt('r'))
            br = ET.SubElement(run, qt('br'))
            br.set(qt('type'), 'page')

            insert_at = len(q_body)   # 默认 append
            sect_pr = q_body.find(qt('sectPr'))
            if sect_pr is not None:
                insert_at = list(q_body).index(sect_pr)
            for i, child in enumerate(a_body):
                q_body.insert(insert_at + i, copy.deepcopy(child))
            q_body.insert(insert_at, page_break_p)

            # 4) 合并关系表
            if q_rels_root is None:
                q_rels_root = ET.Element(f'{{{cls._NS_PKG}}}Relationships')
            for new_id, rtype, rtarget, mode in new_rels:
                rel = ET.SubElement(q_rels_root, f'{{{cls._NS_PKG}}}Relationship')
                rel.set('Id', new_id)
                rel.set('Type', rtype)
                rel.set('Target', rtarget)
                if mode == 'External':
                    rel.set('TargetMode', 'External')

            # 5) [Content_Types].xml 补充新媒体的 Default 声明
            ct_data = q_files.get('[Content_Types].xml')
            if ct_data is not None and copied_files:
                ct_root = ET.fromstring(ct_data)
                ct_default = f'{{{cls._NS_CT}}}Default'
                existing_exts = {
                    e.get('Extension', '').lower()
                    for e in ct_root.findall(ct_default) if e.get('Extension')
                }
                new_exts = set()
                for out_name in copied_files.values():
                    ext = os.path.splitext(out_name)[1].lstrip('.').lower()
                    if ext and ext not in existing_exts:
                        new_exts.add(ext)
                for ext in new_exts:
                    d = ET.SubElement(ct_root, ct_default)
                    d.set('Extension', ext)
                    d.set('ContentType', cls._IMG_CONTENT_TYPES.get(ext, 'application/octet-stream'))
                q_files['[Content_Types].xml'] = cls._serialize_xml(ct_root)

            # 6) 写回输出 zip
            q_files['word/document.xml'] = cls._serialize_xml(q_doc)
            q_files[rels_path] = cls._serialize_xml(q_rels_root)
            for src, out_name in copied_files.items():
                q_files[out_name] = a_files[src]

            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, data in q_files.items():
                    z.writestr(name, data)
            return True

        except Exception as e:
            raise RuntimeError(f"文档合并失败: {e}") from e
