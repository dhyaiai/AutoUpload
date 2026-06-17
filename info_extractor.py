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
    GRADE_PATTERN = re.compile(r'^(.+?)(高一|高二|高三|初一|初二|初三)$')
    
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
    def _read_pdf(file_path: str) -> str:
        """
        读取PDF文件的前200个字符
        使用pdfplumber库逐页读取,累加到200字符
        
        Args:
            file_path: PDF文件路径
        
        Returns:
            前200个字符
        """
        try:
            import pdfplumber
            
            text_parts = []
            current_length = 0
            
            # 打开PDF文件
            with pdfplumber.open(file_path) as pdf:
                # 逐页提取文本
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                        current_length += len(page_text)
                        if current_length >= InfoExtractor.TEXT_EXTRACT_LENGTH:
                            break
            
            # 合并所有文本并截取前200字符
            full_text = ''.join(text_parts)
            return full_text[:InfoExtractor.TEXT_EXTRACT_LENGTH]
        
        except ImportError:
            print("错误: 未安装pdfplumber库,请运行: pip install pdfplumber")
            return ""
