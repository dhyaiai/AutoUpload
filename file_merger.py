"""
文件合并模块
功能: 将试题文件和答案文件合并为一个文件（试题在前，答案在后）
支持: .doc/.docx 使用 Word 原生 COM 调用（Microsoft Word / WPS）
      .pdf 使用 pypdf 库合并
"""
import os


class FileMerger:
    """文件合并器 — 静态方法集合，无需实例化"""

    SUPPORTED_EXTENSIONS = ('.doc', '.docx', '.pdf')

    # Word COM 常量
    WD_STORY = 6          # wdStory — 文档全文范围
    WD_PAGE_BREAK = 7     # wdPageBreak — 分页符
    WD_COLLAPSE_END = 0   # wdCollapseEnd

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

        if q_fmt in ('.doc', '.docx'):
            return cls._merge_word(question_path, answer_path, output_path)
        elif q_fmt == '.pdf':
            return cls._merge_pdf(question_path, answer_path, output_path)

    # ==================== Word COM 合并 ====================

    @classmethod
    def _merge_word(cls, question_path: str, answer_path: str, output_path: str) -> bool:
        """
        使用 Word COM 合并 .doc / .docx 文件。
        自动探测 Microsoft Word 和 WPS，优先复用已运行实例。

        合并策略：新建空白文档 → 插入试题内容 →
        插入分页符 → 插入答案内容 → 另存为。
        """
        try:
            import win32com.client as win32
        except ImportError:
            raise ImportError("需要安装 pywin32，请运行: pip install pywin32")

        word = None
        # 按优先级尝试: MS Word → WPS 个人版 → WPS 专业版
        prog_ids = ['Word.Application', 'WPS.Application', 'KWPS.Application']

        try:
            # 先尝试连接已运行的实例
            for pid in prog_ids:
                try:
                    word = win32.GetObject(Class=pid)
                    break
                except Exception:
                    continue

            # 没有已运行实例则启动新实例
            if word is None:
                for pid in prog_ids:
                    try:
                        word = win32.Dispatch(pid)
                        break
                    except Exception:
                        continue

            if word is None:
                raise RuntimeError(
                    "未找到可用的 Word 或 WPS 程序。\n"
                    "请确认已安装 Microsoft Word 或 WPS Office 文字组件。"
                )

            word.Visible = False
            word.DisplayAlerts = 0  # 抑制弹窗

            # 创建新文档
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
            raise RuntimeError(f"Word 文档合并失败: {e}") from e

        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass

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
