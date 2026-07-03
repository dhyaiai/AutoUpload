"""
AI科目识别模块
功能:优先从文件名正则匹配科目,匹配不到再调用DeepSeek API根据文件内容识别
特点:支持重试机制,温度参数设为0保证输出稳定
"""
import re
import requests
import time
from typing import Optional
from config_manager import ConfigManager


class SubjectClassifier:
    """
    科目分类器
    优先从文件名提取科目,命中则直接返回;否则使用DeepSeek AI API进行科目识别
    """

    # 九科科目名称列表
    SUBJECTS = ['语文', '数学', '英语', '物理', '化学', '生物', '历史', '地理', '政治']

    # 从文件名匹配科目的正则:匹配九科中任一科目名
    SUBJECT_FROM_FILENAME_PATTERN = re.compile(
        '|'.join(SUBJECTS)
    )

    # DeepSeek API地址
    API_URL = "https://api.deepseek.com/v1/chat/completions"

    # 系统提示词:指导AI如何分类
    SYSTEM_PROMPT = """你是一个科目分类助手。根据提供的作业内容前200字,判断它属于哪个科目。
只回复科目名称:语文、数学、英语、物理、化学、生物、历史、地理、政治。"""
    
    def __init__(self):
        """
        初始化科目分类器
        从配置管理器获取API密钥和重试次数
        """
        self.config = ConfigManager()
        self.api_key = self.config.deepseek_api_key
        self.max_retries = self.config.max_retry_count
    
    @staticmethod
    def extract_subject_from_filename(file_name: str) -> Optional[str]:
        """
        从文件名中正则匹配科目名称

        Args:
            file_name: 文件名(含扩展名或不含均可)

        Returns:
            科目名称(如"数学"),匹配不到返回None
        """
        match = SubjectClassifier.SUBJECT_FROM_FILENAME_PATTERN.search(file_name)
        if match:
            subject = match.group()
            print(f"从文件名识别到科目: {subject}")
            return subject
        return None

    def classify(self, text: str, file_name: str = None) -> Optional[str]:
        """
        识别文本所属的科目
        优先从文件名提取,命中则直接返回;否则调用AI识别

        Args:
            text: 文件内容的前200个字符
            file_name: 文件名(可选),用于优先从文件名匹配科目

        Returns:
            科目名称(如"数学"),如果识别失败返回None
        """
        # 优先从文件名匹配科目
        if file_name:
            subject = self.extract_subject_from_filename(file_name)
            if subject:
                return subject

        # 如果文本为空,直接返回None
        if not text or not text.strip():
            print("警告: 文本内容为空,无法识别科目")
            return None
        
        # 如果没有配置API密钥,返回None
        if not self.api_key:
            print("错误: 未配置DeepSeek API密钥")
            return None
        
        # 尝试调用API,支持重试
        for attempt in range(1, self.max_retries + 1):
            try:
                subject = self._call_api(text)
                if subject:
                    print(f"成功识别科目: {subject}")
                    return subject
                else:
                    print(f"警告: 第{attempt}次尝试识别科目失败")
            
            except Exception as e:
                print(f"错误: 第{attempt}次API调用异常 - {e}")
            
            # 如果不是最后一次尝试,等待2秒后重试
            if attempt < self.max_retries:
                print(f"等待2秒后重试...")
                time.sleep(2)
        
        # 所有重试都失败
        print(f"错误: 经过{self.max_retries}次尝试仍无法识别科目")
        return None
    
    def _call_api(self, text: str) -> Optional[str]:
        """
        调用DeepSeek API进行科目识别
        
        Args:
            text: 要分析的文本内容
        
        Returns:
            识别出的科目名称,失败返回None
        
        Raises:
            requests.exceptions.RequestException: 网络请求异常
        """
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        # 构建请求体
        payload = {
            "model": "deepseek-chat",           # 使用的模型
            "messages": [
                {
                    "role": "system",
                    "content": self.SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": f"文件内容前200字:{text}"
                }
            ],
            "temperature": 0  # 温度设为0,保证输出稳定一致
        }
        
        # 发送POST请求
        response = requests.post(
            self.API_URL,
            headers=headers,
            json=payload,
            timeout=30  # 超时时间30秒
        )
        
        # 检查响应状态码
        response.raise_for_status()
        
        # 解析JSON响应
        result = response.json()
        
        # 提取AI回复的内容
        if "choices" in result and len(result["choices"]) > 0:
            subject = result["choices"][0]["message"]["content"].strip()
            # 清理可能的标点符号（英文冒号、中文冒号）
            subject = subject.replace(":", "").replace("：", "").strip()
            return subject if subject else None
        else:
            return None
