"""
信息提取模块
功能:从文件路径中提取学校和年级信息,读取文件前200字内容
支持的文件格式:txt、docx、pdf
"""
import re
import os
from typing import Tuple, Optional


class InfoExtractor:
    """
    信息提取器
    负责解析文件夹名称获取学校年级,以及读取文件内容
    """
    
    # 年级匹配正则表达式
    # 匹配模式:任意字符 + (高一|高二|...|小六)
    GRADE_PATTERN = re.compile(r'^(.+?)(高一|高二|高三|七年级|八年级|九年级|一年级|二年级|三年级|四年级|五年级|六年级)$')
    
    # 需要提取的文本长度(字符数)
    TEXT_EXTRACT_LENGTH = 200
    
    @staticmethod
    def parse_folder_name(folder_path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从文件夹路径中解析学校名称和年级
        
        Args:
            folder_path: 文件夹的完整路径或文件夹名称
        
        Returns:
            元组 (学校名称, 年级)
            如果解析失败,返回 (None, None)
        
        Examples:
            >>> parse_folder_name("合肥卓越中学高一")
            ('合肥卓越中学', '高一')
            >>> parse_folder_name("invalid")
            (None, None)
        """
        # 如果传入的是完整路径,提取文件夹名称
        folder_name = os.path.basename(folder_path)
        
        # 使用正则表达式匹配
        match = InfoExtractor.GRADE_PATTERN.match(folder_name)
        
        if match:
            school = match.group(1)  # 第一组:学校名称
            grade = match.group(2)   # 第二组:年级
            return school, grade
        else:
            # 匹配失败,返回None
            return None, None
    
    @staticmethod
    def read_file_content(file_path: str) -> str:
        """
        读取文件的前200个字符内容
        根据文件扩展名自动选择对应的读取方法
        
        Args:
            file_path: 文件的完整路径
        
        Returns:
            文件的前200个字符,如果读取失败返回空字符串
        """
        # 获取文件扩展名(转为小写)
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        
        try:
            if ext == '.txt':
                return InfoExtractor._read_txt(file_path)
            elif ext == '.docx':
                return InfoExtractor._read_docx(file_path)
            elif ext == '.doc':
                return InfoExtractor._read_doc(file_path)
            elif ext == '.pdf':
                return InfoExtractor._read_pdf(file_path)
            else:
                # 不支持的文件格式
                print(f"警告: 不支持的文件格式 {ext}")
                return ""
        except Exception as e:
            print(f"错误: 读取文件失败 {file_path} - {e}")
            return ""
    
    @staticmethod
    def _read_txt(file_path: str) -> str:
        """
        读取TXT文件的前200个字符
        
        Args:
            file_path: TXT文件路径
        
        Returns:
            前200个字符
        """
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(InfoExtractor.TEXT_EXTRACT_LENGTH)
        return content
    
    @staticmethod
    def _read_docx(file_path: str) -> str:
        """
        读取DOCX文件的前200个字符
        使用python-docx库逐段读取,累加到200字符
        
        Args:
            file_path: DOCX文件路径
        
        Returns:
            前200个字符
        """
        try:
            from docx import Document
            
            doc = Document(file_path)
            text_parts = []
            current_length = 0
            
            # 遍历所有段落,累加文本直到达到200字符
            for paragraph in doc.paragraphs:
                para_text = paragraph.text
                if para_text:
                    text_parts.append(para_text)
                    current_length += len(para_text)
                    if current_length >= InfoExtractor.TEXT_EXTRACT_LENGTH:
                        break
            
            # 合并所有文本并截取前200字符
            full_text = ''.join(text_parts)
            return full_text[:InfoExtractor.TEXT_EXTRACT_LENGTH]
        
        except ImportError:
            print("错误: 未安装python-docx库,请运行: pip install python-docx")
            return ""
    
    @staticmethod
    def _read_doc(file_path: str) -> str:
        """
        读取DOC文件(Word 97-2003格式)的前200个字符
        使用olefile解析OLE2复合文档，从WordDocument流中提取文本

        Args:
            file_path: DOC文件路径

        Returns:
            前200个字符
        """
        try:
            import olefile

            ole = olefile.OleFileIO(file_path)
            try:
                # .doc 文件必须包含 WordDocument 流
                if not ole.exists('WordDocument'):
                    print("警告: .doc文件中未找到WordDocument流")
                    return ""

                data = ole.openstream('WordDocument').read()
            finally:
                ole.close()

            # Word二进制格式的文本提取：
            # .doc文件的文本在WordDocument流中，按piece table存储
            # 简化方案：尝试UTF-16LE解码后过滤可读字符
            text_candidates = []

            # 方案A: UTF-16LE解码（多数中文.doc文件使用）
            try:
                decoded = data.decode('utf-16-le', errors='ignore')
                # 保留中文、英文、数字、标点和常见空白
                import re
                cleaned = re.sub(r'[^一-鿿　-〿＀-￯a-zA-Z0-9\s.,;:!?()（）、。，；：！？""''【】《》+=-]', '', decoded)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) > 20:
                    text_candidates.append(cleaned)
            except Exception:
                pass

            # 方案B: 原始二进制中提取连续可打印ASCII+中文序列
            try:
                import re
                # 将bytes中可打印的ASCII和UTF-8中文序列提取出来
                text = data.decode('utf-8', errors='ignore')
                cleaned = re.sub(r'[^一-鿿　-〿＀-￯a-zA-Z0-9\s.,;:!?()（）]', '', text)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) > 20:
                    text_candidates.append(cleaned)
            except Exception:
                pass

            # 方案C: Latin-1 + 中文区域检测
            if not text_candidates:
                try:
                    text = data.decode('latin-1', errors='ignore')
                    import re
                    cleaned = re.sub(r'[^\x20-\x7E一-鿿]', '', text)
                    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                    if len(cleaned) > 20:
                        text_candidates.append(cleaned)
                except Exception:
                    pass

            # 返回最长的有效文本
            if text_candidates:
                best = max(text_candidates, key=len)
                return best[:InfoExtractor.TEXT_EXTRACT_LENGTH]

            return ""

        except ImportError:
            print("警告: 未安装olefile库，无法读取.doc文件。请运行: pip install olefile")
            return ""
        except Exception as e:
            print(f"错误: 读取.doc文件失败 {file_path} - {e}")
            return ""

    @staticmethod
    def _read_pdf(file_path: str) -> str:
        """
        读取PDF文件的前200个字符
        使用pypdf库逐页读取,累加到200字符

        Args:
            file_path: PDF文件路径

        Returns:
            前200个字符
        """
        try:
            from pypdf import PdfReader

            text_parts = []
            current_length = 0

            # 打开PDF文件(pypdf无上下文管理器)
            reader = PdfReader(file_path)
            # 逐页提取文本(pypdf的extract_text()可能返回None)
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text:
                    text_parts.append(page_text)
                    current_length += len(page_text)
                    if current_length >= InfoExtractor.TEXT_EXTRACT_LENGTH:
                        break

            # 合并所有文本并截取前200字符
            full_text = ''.join(text_parts)
            return full_text[:InfoExtractor.TEXT_EXTRACT_LENGTH]

        except ImportError:
            print("错误: 未安装pypdf库,请运行: pip install pypdf")
            return ""
        except Exception as e:
            print(f"错误: 读取PDF文件失败 {file_path} - {e}")
            return ""
