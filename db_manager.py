"""
数据库管理模块
功能:负责SQLite数据库的初始化、连接管理和所有数据操作
特点:使用单例模式确保全局只有一个数据库连接
"""
import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional


class DatabaseManager:
    """
    数据库管理器(单例模式)
    确保整个程序中只有一个数据库连接实例,避免资源浪费和数据冲突
    """
    
    # 类变量,用于存储唯一实例和数据库连接
    _instance = None
    _connection = None
    
    def __new__(cls, db_path: str = "data.db"):
        """
        重写__new__方法实现单例模式
        如果实例不存在则创建,存在则返回已有实例
        
        Args:
            db_path: 数据库文件路径,默认为当前目录下的data.db
        """
        if cls._instance is None:
            # 首次调用,创建新实例
            cls._instance = super().__new__(cls)
            cls._instance.db_path = db_path
            cls._instance._init_db()
        return cls._instance
    
    def _init_db(self):
        """
        初始化数据库连接和表结构
        - 建立SQLite连接
        - 设置行工厂为sqlite3.Row,支持字典式访问
        - 创建必要的数据表
        """
        # 建立数据库连接,check_same_thread=False允许跨线程使用
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        # 设置行工厂,使查询结果可以像字典一样通过列名访问
        self._connection.row_factory = sqlite3.Row
        # 创建数据表
        self._create_tables()
    
    def _create_tables(self):
        """
        创建上传记录表
        表结构包含:文件信息、学校年级科目、上传状态、错误信息、重试次数等
        """
        cursor = self._connection.cursor()
        
        # 创建上传记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键
                file_name TEXT NOT NULL,                -- 文件名
                file_path TEXT NOT NULL,                -- 文件完整路径
                folder_name TEXT NOT NULL,              -- 文件夹名称
                school TEXT NOT NULL,                   -- 学校名称
                grade TEXT NOT NULL,                    -- 年级
                subject TEXT NOT NULL,                  -- 科目
                status TEXT NOT NULL DEFAULT 'success', -- 状态:'success'成功 或 'failed'失败
                error_message TEXT,                     -- 失败时的错误信息
                retry_count INTEGER DEFAULT 0,          -- 重试次数
                upload_time DATETIME DEFAULT CURRENT_TIMESTAMP  -- 上传时间,默认当前时间
            )
        ''')
        
        # 创建索引,优化常用查询的性能
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_name ON upload_records(file_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON upload_records(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_folder_name ON upload_records(folder_name)')

        # 创建数据分析表(结构与upload_records一致，用于存放用户手动复制的快照数据)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                folder_name TEXT NOT NULL,
                school TEXT NOT NULL,
                grade TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'success',
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                upload_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_analysis_subject ON analysis_records(subject)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_analysis_school ON analysis_records(school)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_analysis_grade ON analysis_records(grade)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_analysis_upload_time ON analysis_records(upload_time)')

        # 提交事务
        self._connection.commit()
    
    def add_record(self, file_name: str, file_path: str, folder_name: str,
                   school: str, grade: str, subject: str, 
                   status: str = 'success', error_message: str = None) -> int:
        """
        添加上传记录到数据库
        
        Args:
            file_name: 文件名(不含路径)
            file_path: 文件的完整路径
            folder_name: 所在文件夹名称
            school: 学校名称
            grade: 年级(如:高一、初二等)
            subject: 科目(如:数学、语文等)
            status: 上传状态,'success'表示成功,'failed'表示失败
            error_message: 失败时的错误描述信息
        
        Returns:
            新插入记录的ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            INSERT INTO upload_records
            (file_name, file_path, folder_name, school, grade, subject, status, error_message, upload_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (file_name, file_path, folder_name, school, grade, subject, status, error_message,
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        self._connection.commit()
        return cursor.lastrowid
    
    def is_file_uploaded(self, file_name: str, folder_name: str = None) -> bool:
        """
        检查文件是否已经成功上传过(防止重复上传)

        Args:
            file_name: 要检查的文件名
            folder_name: 文件夹名称,提供时同时按文件名+文件夹名精确匹配

        Returns:
            True表示该文件已成功上传过,False表示未上传或上传失败
        """
        cursor = self._connection.cursor()
        if folder_name:
            cursor.execute('''
                SELECT COUNT(*) FROM upload_records
                WHERE file_name = ? AND folder_name = ? AND status = 'success'
            ''', (file_name, folder_name))
        else:
            cursor.execute('''
                SELECT COUNT(*) FROM upload_records
                WHERE file_name = ? AND status = 'success'
            ''', (file_name,))
        count = cursor.fetchone()[0]
        return count > 0
    
    def get_failed_records(self) -> List[Dict]:
        """
        获取所有上传失败的记录
        
        Returns:
            失败记录列表,每条记录是一个字典,按上传时间倒序排列
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT * FROM upload_records 
            WHERE status = 'failed'
            ORDER BY upload_time DESC
        ''')
        rows = cursor.fetchall()
        # 将每行转换为字典格式,方便GUI显示
        return [dict(row) for row in rows]
    
    def mark_record_success(self, record_id: int):
        """
        将某条失败记录标记为成功(用于重新上传成功后更新状态)
        
        Args:
            record_id: 要更新的记录ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            UPDATE upload_records
            SET status = 'success',
                error_message = NULL,
                retry_count = 0,
                upload_time = ?
            WHERE id = ?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), record_id))
        self._connection.commit()
    
    def increment_retry(self, record_id: int) -> int:
        """
        增加记录的重试次数
        
        Args:
            record_id: 记录ID
        
        Returns:
            更新后的重试次数
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            UPDATE upload_records 
            SET retry_count = retry_count + 1
            WHERE id = ?
        ''', (record_id,))
        self._connection.commit()
        
        # 查询更新后的重试次数
        cursor.execute('SELECT retry_count FROM upload_records WHERE id = ?', (record_id,))
        return cursor.fetchone()[0]
    
    def delete_records_by_folder(self, folder_name: str):
        """
        删除指定文件夹下的所有上传记录(用于清空或删除文件夹时同步清理数据库)
        
        Args:
            folder_name: 文件夹名称
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            DELETE FROM upload_records 
            WHERE folder_name = ?
        ''', (folder_name,))
        self._connection.commit()
    
    def delete_record(self, record_id: int):
        """
        删除单条记录(用于用户忽略某个失败文件)
        
        Args:
            record_id: 要删除的记录ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            DELETE FROM upload_records 
            WHERE id = ?
        ''', (record_id,))
        self._connection.commit()
    
    def update_error_message(self, record_id: int, error_message: str):
        """
        更新记录的错误信息(用于重新上传失败后更新错误原因)
        
        Args:
            record_id: 记录ID
            error_message: 新的错误信息
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            UPDATE upload_records 
            SET error_message = ?
            WHERE id = ?
        ''', (error_message, record_id))
        self._connection.commit()
    
    # ==================== 数据分析相关方法 ====================

    def copy_success_to_analysis(self) -> int:
        """
        将所有成功上传的记录复制到分析表(先清空再复制,实现快照功能)

        Returns:
            复制的记录数
        """
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM analysis_records")
        cursor.execute('''
            INSERT INTO analysis_records
            (file_name, file_path, folder_name, school, grade, subject,
             status, error_message, retry_count, upload_time)
            SELECT file_name, file_path, folder_name, school, grade, subject,
                   status, error_message, retry_count, upload_time
            FROM upload_records WHERE status = 'success'
        ''')
        self._connection.commit()
        return cursor.rowcount

    def add_analysis_record(self, file_name: str, file_path: str, folder_name: str,
                            school: str, grade: str, subject: str,
                            status: str = 'success', error_message: str = None) -> int:
        """
        直接插入一条记录到分析表（上传成功时自动同步，无需手动复制）

        Args:
            file_name: 文件名(不含路径)
            file_path: 文件的完整路径
            folder_name: 所在文件夹名称
            school: 学校名称
            grade: 年级
            subject: 科目
            status: 上传状态
            error_message: 失败时的错误描述信息

        Returns:
            新插入记录的ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            INSERT INTO analysis_records
            (file_name, file_path, folder_name, school, grade, subject, status, error_message, upload_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (file_name, file_path, folder_name, school, grade, subject, status, error_message,
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        self._connection.commit()
        return cursor.lastrowid

    def get_all_successful_records(self) -> List[Dict]:
        """获取所有上传成功的记录,按上传时间倒序"""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT * FROM upload_records WHERE status = 'success' ORDER BY upload_time DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_all_analysis_records(self) -> List[Dict]:
        """获取分析表中所有记录,按上传时间倒序"""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT * FROM analysis_records ORDER BY upload_time DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_failed_records_for_stats(self) -> List[Dict]:
        """获取失败记录的关键字段,用于统计面板显示"""
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT file_name, school, grade, subject, upload_time, error_message
            FROM upload_records WHERE status = 'failed'
            ORDER BY upload_time DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]

    def get_upload_count_by_subject(self) -> List[Dict]:
        """从分析表按科目统计上传数量,降序排列"""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT subject, COUNT(*) as count FROM analysis_records "
            "GROUP BY subject ORDER BY count DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_upload_count_by_school_grade(self) -> List[Dict]:
        """从分析表按学校+年级统计上传数量,降序排列"""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT school, grade, COUNT(*) as count FROM analysis_records "
            "GROUP BY school, grade ORDER BY count DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_upload_count_by_date(self, aggregation: str = "daily") -> List[Dict]:
        """
        从分析表按日期统计上传数量

        Args:
            aggregation: 'daily'按日 / 'weekly'按周 / 'monthly'按月

        Returns:
            [{"date_label": "2026-06-15", "count": 7}, ...]
        """
        cursor = self._connection.cursor()
        if aggregation == "weekly":
            cursor.execute('''
                SELECT strftime('%Y-%W', upload_time) as date_label,
                       MIN(DATE(upload_time)) as sort_key,
                       COUNT(*) as count
                FROM analysis_records GROUP BY date_label ORDER BY sort_key
            ''')
        elif aggregation == "monthly":
            cursor.execute('''
                SELECT strftime('%Y-%m', upload_time) as date_label,
                       COUNT(*) as count
                FROM analysis_records GROUP BY date_label ORDER BY date_label
            ''')
        else:  # daily
            cursor.execute('''
                SELECT DATE(upload_time) as date_label,
                       COUNT(*) as count
                FROM analysis_records GROUP BY date_label ORDER BY date_label
            ''')
        return [dict(row) for row in cursor.fetchall()]

    def clear_analysis_table(self):
        """清空分析表"""
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM analysis_records")
        self._connection.commit()

    def close(self):
        """
        关闭数据库连接
        在程序退出时调用,释放资源
        """
        if self._connection:
            self._connection.close()
            self._connection = None
            DatabaseManager._instance = None
