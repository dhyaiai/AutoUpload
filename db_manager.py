"""
数据库管理模块
功能:负责SQLite数据库的初始化、连接管理和所有数据操作
特点:使用单例模式确保全局只有一个数据库连接
"""
import sqlite3
import os
from datetime import datetime, timedelta
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
                upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,  -- 上传时间,默认当前时间
                fail_stage TEXT DEFAULT NULL,           -- 失败阶段(UploadStage枚举值)
                error_category TEXT DEFAULT NULL,       -- 错误一级分类(ErrorCategory枚举值)
                error_type TEXT DEFAULT NULL,           -- 错误二级类型(ErrorType枚举值)
                error_context TEXT DEFAULT NULL,        -- 错误上下文JSON(页面URL/元素信息等)
                retry_status TEXT DEFAULT 'pending',    -- 重试处理状态:pending/processing/finished
                agent_retry_success TEXT DEFAULT NULL,  -- Agent接管是否成功: '是'/'否'
                source TEXT DEFAULT 'desktop'           -- 任务来源: desktop/miniprogram
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

        # 执行表结构迁移（兼容旧数据库，新增字段不存在时自动添加）
        self._migrate_schema()

        # 提交事务
        self._connection.commit()

    def _migrate_schema(self):
        """
        兼容旧数据库：检查并添加新增字段，缺失则自动 ALTER TABLE
        """
        new_columns = {
            'fail_stage': 'TEXT DEFAULT NULL',
            'error_category': 'TEXT DEFAULT NULL',
            'error_type': 'TEXT DEFAULT NULL',
            'error_context': 'TEXT DEFAULT NULL',
            'retry_status': "TEXT DEFAULT 'pending'",
            'agent_retry_success': 'TEXT DEFAULT NULL',
            'source': "TEXT DEFAULT 'desktop'",
        }
        cursor = self._connection.cursor()
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(upload_records)").fetchall()}
        for col_name, col_def in new_columns.items():
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE upload_records ADD COLUMN {col_name} {col_def}")
                except Exception as e:
                    print(f"数据库迁移警告 (添加列 {col_name}): {e}")
    
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
    
    def get_failed_records(self, page: int = None, size: int = None) -> List[Dict]:
        """
        获取所有上传失败的记录（支持可选分页）

        Args:
            page: 页码(从1开始), None=返回全部
            size: 每页条数, None=返回全部

        Returns:
            失败记录列表,每条记录是一个字典,按上传时间倒序排列
        """
        cursor = self._connection.cursor()
        if page is not None and size is not None:
            offset = (page - 1) * size
            cursor.execute('''
                SELECT * FROM upload_records
                WHERE status = 'failed'
                ORDER BY upload_time DESC
                LIMIT ? OFFSET ?
            ''', (size, offset))
        else:
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

    def mark_record_failed(self, record_id: int):
        """
        将记录标记为失败（用于 miniprogram 任务处理失败时将 pending 改为 failed）

        Args:
            record_id: 要更新的记录ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            UPDATE upload_records
            SET status = 'failed'
            WHERE id = ?
        ''', (record_id,))
        self._connection.commit()

    def resolve_pending_by_file(self, file_name: str, folder_name: str) -> int:
        """
        将指定文件的所有 pending 失败记录标记为 success+finished。
        用于上传成功后清理旧失败记录，防止 Agent 重复扫描。

        Args:
            file_name: 文件名
            folder_name: 文件夹名

        Returns:
            被清理的记录数量
        """
        cursor = self._connection.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            UPDATE upload_records
            SET status = 'success',
                retry_status = 'finished',
                error_message = NULL,
                retry_count = 0,
                upload_time = ?
            WHERE file_name = ? AND folder_name = ?
              AND status = 'failed'
              AND retry_status = 'pending'
        ''', (now, file_name, folder_name))
        self._connection.commit()
        return cursor.rowcount
    
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
        """获取失败记录的关键字段,用于统计面板显示(从分析表读取,持久保留)"""
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT file_name, school, grade, subject, upload_time, error_message
            FROM analysis_records WHERE status = 'failed'
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

    # ==================== Agent 相关新增接口 ====================

    def add_failed_record_structured(self, file_name: str, file_path: str, folder_name: str,
                                     school: str, grade: str, subject: str,
                                     error_message: str = None,
                                     fail_stage: str = None,
                                     error_category: str = None,
                                     error_type: str = None,
                                     error_context: str = None) -> int:
        """
        结构化写入失败记录（含失败阶段、错误分类、上下文）

        Args:
            file_name: 文件名
            file_path: 文件路径
            folder_name: 文件夹名称
            school: 学校
            grade: 年级
            subject: 科目
            error_message: 错误描述
            fail_stage: UploadStage 枚举值
            error_category: ErrorCategory 枚举值
            error_type: ErrorType 枚举值
            error_context: JSON 格式的上下文信息

        Returns:
            新插入记录的ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            INSERT INTO upload_records
            (file_name, file_path, folder_name, school, grade, subject,
             status, error_message, fail_stage, error_category, error_type,
             error_context, retry_status, upload_time)
            VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?, 'pending', ?)
        ''', (file_name, file_path, folder_name, school, grade, subject,
              error_message, fail_stage, error_category, error_type,
              error_context, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        self._connection.commit()
        return cursor.lastrowid

    def get_pending_failed_records(self, limit: int = 20) -> List[Dict]:
        """
        获取待处理的失败记录（retry_status='pending' 且 status='failed'）
        按 upload_time 升序（先失败的先处理）

        Args:
            limit: 最大返回数量

        Returns:
            失败记录列表，每条记录是一个字典
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT * FROM upload_records
            WHERE status = 'failed' AND retry_status = 'pending'
            ORDER BY upload_time ASC
            LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def count_pending_retry_records(self) -> int:
        """
        统计待处理的失败记录数量（retry_status='pending'）
        用于判断是否有 Agent 重试任务挂起，避免浏览器空闲关闭

        Returns:
            待处理失败记录数
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE status = 'failed' AND retry_status = 'pending'
        ''')
        row = cursor.fetchone()
        return row[0] if row else 0

    def update_retry_status(self, record_id: int, status: str):
        """
        更新记录的重试处理状态

        Args:
            record_id: 记录ID
            status: 'pending' / 'processing' / 'finished'
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            UPDATE upload_records SET retry_status = ? WHERE id = ?
        ''', (status, record_id))
        self._connection.commit()

    def update_record_structured_error(self, record_id: int,
                                       error_message: str = None,
                                       fail_stage: str = None,
                                       error_category: str = None,
                                       error_type: str = None,
                                       error_context: str = None):
        """
        更新记录的失败阶段和错误分类信息
        用于 retry 后更新错误详情

        Args:
            record_id: 记录ID
            error_message: 新的错误信息
            fail_stage: UploadStage 枚举值
            error_category: ErrorCategory 枚举值
            error_type: ErrorType 枚举值
            error_context: JSON 格式上下文
        """
        cursor = self._connection.cursor()
        updates = []
        values = []
        if error_message is not None:
            updates.append("error_message = ?")
            values.append(error_message)
        if fail_stage is not None:
            updates.append("fail_stage = ?")
            values.append(fail_stage)
        if error_category is not None:
            updates.append("error_category = ?")
            values.append(error_category)
        if error_type is not None:
            updates.append("error_type = ?")
            values.append(error_type)
        if error_context is not None:
            updates.append("error_context = ?")
            values.append(error_context)
        if not updates:
            return
        values.append(record_id)
        cursor.execute(f"UPDATE upload_records SET {', '.join(updates)} WHERE id = ?", values)
        self._connection.commit()

    def set_agent_retry_success(self, record_id: int, success: bool):
        """
        标记 Agent 接管重试是否成功

        Args:
            record_id: 记录ID
            success: True=成功, False=失败
        """
        cursor = self._connection.cursor()
        value = '是' if success else '否'
        cursor.execute(
            "UPDATE upload_records SET agent_retry_success = ? WHERE id = ?",
            (value, record_id)
        )
        self._connection.commit()

    def get_failed_stats_by_period(self, start_time: str, end_time: str) -> Dict:
        """
        按时间范围聚合失败统计数据

        Args:
            start_time: 起始时间 'YYYY-MM-DD HH:MM:SS'
            end_time: 截止时间 'YYYY-MM-DD HH:MM:SS'

        Returns:
            包含各项统计指标的字典
        """
        cursor = self._connection.cursor()

        # 总上传量
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
        ''', (start_time, end_time))
        total_uploads = cursor.fetchone()[0]

        # 失败数
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE upload_time BETWEEN ? AND ? AND status = 'failed'
        ''', (start_time, end_time))
        total_failed = cursor.fetchone()[0]

        # Agent 挽回数（agent_retry_success='是'）
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            AND status = 'success' AND agent_retry_success = '是'
        ''', (start_time, end_time))
        agent_recovered = cursor.fetchone()[0]

        # 重试成功数（retry_count > 0 且最终 status='success'）
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            AND status = 'success' AND retry_count > 0
        ''', (start_time, end_time))
        retry_success = cursor.fetchone()[0]

        # 待人工处理数（retry_status='finished' 且 status='failed'）
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            AND status = 'failed' AND retry_status = 'finished'
        ''', (start_time, end_time))
        manual_pending = cursor.fetchone()[0]

        # 按 error_category 分布
        cursor.execute('''
            SELECT error_category, COUNT(*) as cnt FROM upload_records
            WHERE upload_time BETWEEN ? AND ? AND status = 'failed'
            GROUP BY error_category ORDER BY cnt DESC
        ''', (start_time, end_time))
        category_distribution = [dict(row) for row in cursor.fetchall()]

        # 按 error_type 分布
        cursor.execute('''
            SELECT error_type, COUNT(*) as cnt FROM upload_records
            WHERE upload_time BETWEEN ? AND ? AND status = 'failed'
            GROUP BY error_type ORDER BY cnt DESC
        ''', (start_time, end_time))
        type_distribution = [dict(row) for row in cursor.fetchall()]

        return {
            'total_uploads': total_uploads,
            'total_failed': total_failed,
            'agent_recovered': agent_recovered,
            'retry_success': retry_success,
            'manual_pending': manual_pending,
            'category_distribution': category_distribution,
            'type_distribution': type_distribution,
        }

    def get_failed_records_by_period(self, start_time: str, end_time: str) -> List[Dict]:
        """
        获取指定周期的所有失败记录

        Args:
            start_time: 起始时间
            end_time: 截止时间

        Returns:
            失败记录列表
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT * FROM upload_records
            WHERE upload_time BETWEEN ? AND ? AND status = 'failed'
            ORDER BY upload_time DESC
        ''', (start_time, end_time))
        return [dict(row) for row in cursor.fetchall()]

    def get_all_records_by_period(self, start_time: str, end_time: str) -> List[Dict]:
        """
        获取指定周期的所有上传记录（含成功和失败）

        Args:
            start_time: 起始时间
            end_time: 截止时间

        Returns:
            记录列表
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT * FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            ORDER BY upload_time DESC
        ''', (start_time, end_time))
        return [dict(row) for row in cursor.fetchall()]

    def get_error_type_retry_stats(self, start_time: str = None, end_time: str = None) -> List[Dict]:
        """
        按错误类型统计重试效果（重试成功率、平均重试次数）

        Args:
            start_time: 可选的时间范围起始
            end_time: 可选的时间范围截止

        Returns:
            [{error_type, total, retry_success_count, avg_retry_count}, ...]
        """
        cursor = self._connection.cursor()
        params = []
        time_condition = ""
        if start_time and end_time:
            time_condition = " AND upload_time BETWEEN ? AND ?"
            params = [start_time, end_time]

        cursor.execute(f'''
            SELECT error_type,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'success' AND retry_count > 0 THEN 1 ELSE 0 END) as retry_success_count,
                   AVG(retry_count) as avg_retry_count
            FROM upload_records
            WHERE (status = 'failed' OR retry_count > 0){time_condition}
            GROUP BY error_type ORDER BY total DESC
        ''', params)
        return [dict(row) for row in cursor.fetchall()]

    def get_failure_rate_by_school_grade(self, start_time: str, end_time: str) -> List[Dict]:
        """按学校+年级统计失败率"""
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT school, grade,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            GROUP BY school, grade ORDER BY failed DESC
        ''', (start_time, end_time))
        return [dict(row) for row in cursor.fetchall()]

    def get_failure_rate_by_subject(self, start_time: str, end_time: str) -> List[Dict]:
        """按科目统计失败率"""
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT subject,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            GROUP BY subject ORDER BY failed DESC
        ''', (start_time, end_time))
        return [dict(row) for row in cursor.fetchall()]

    def get_daily_failure_trend(self, start_time: str, end_time: str) -> List[Dict]:
        """按天统计失败率趋势"""
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT DATE(upload_time) as date_label,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM upload_records
            WHERE upload_time BETWEEN ? AND ?
            GROUP BY date_label ORDER BY date_label
        ''', (start_time, end_time))
        return [dict(row) for row in cursor.fetchall()]

    def get_circuit_breaker_stats(self, error_type: str, minutes: int = 5) -> int:
        """
        获取指定时间窗口内某类错误的失败次数（用于熔断判断）

        Args:
            error_type: ErrorType 枚举值
            minutes: 时间窗口（分钟）

        Returns:
            失败次数
        """
        cursor = self._connection.cursor()
        cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime(
            '%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            SELECT COUNT(*) FROM upload_records
            WHERE error_type = ? AND upload_time >= ? AND status = 'failed'
        ''', (error_type, cutoff))
        return cursor.fetchone()[0]

    # ==================== 微信小程序适配方法 ====================

    def add_pending_record(self, file_name: str, file_path: str, folder_name: str,
                           school: str, grade: str, subject: str,
                           source: str = 'desktop') -> int:
        """
        写入待处理任务记录（初始状态为 pending，供小程序轮询查询）

        Args:
            file_name: 文件名
            file_path: 文件完整路径
            folder_name: 文件夹名（小程序任务统一用 "miniprogram"）
            school: 学校
            grade: 年级
            subject: 科目
            source: 任务来源 desktop/miniprogram

        Returns:
            新插入记录的ID
        """
        cursor = self._connection.cursor()
        cursor.execute('''
            INSERT INTO upload_records
            (file_name, file_path, folder_name, school, grade, subject,
             status, retry_status, upload_time, source)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)
        ''', (file_name, file_path, folder_name, school, grade, subject,
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'), source))
        self._connection.commit()
        return cursor.lastrowid

    def get_record_by_id(self, record_id: int) -> Optional[Dict]:
        """
        根据ID查询单条记录

        Args:
            record_id: 记录ID

        Returns:
            记录字典，不存在则返回 None
        """
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM upload_records WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_failed_count(self) -> int:
        """
        获取失败记录总数

        Returns:
            失败记录数量
        """
        cursor = self._connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM upload_records WHERE status = 'failed'")
        row = cursor.fetchone()
        return row[0] if row else 0

    def get_stats_overview(self) -> Dict:
        """
        获取统计概览数据（总量/成功率/今日数据/待重试数）

        Returns:
            统计概览字典
        """
        cursor = self._connection.cursor()
        today = datetime.now().strftime('%Y-%m-%d')

        # 总上传量（排除 pending 状态，只统计已完成的成功/失败）
        cursor.execute("SELECT COUNT(*) FROM upload_records WHERE status != 'pending'")
        total = cursor.fetchone()[0]

        # 成功数
        cursor.execute("SELECT COUNT(*) FROM upload_records WHERE status = 'success'")
        success = cursor.fetchone()[0]

        # 失败数
        cursor.execute("SELECT COUNT(*) FROM upload_records WHERE status = 'failed'")
        failed = cursor.fetchone()[0]

        # 今日上传量（排除 pending）
        cursor.execute(
            "SELECT COUNT(*) FROM upload_records WHERE status != 'pending' AND DATE(upload_time) = ?", (today,))
        today_total = cursor.fetchone()[0]

        # 今日成功数
        cursor.execute(
            "SELECT COUNT(*) FROM upload_records WHERE status = 'success' AND DATE(upload_time) = ?", (today,))
        today_success = cursor.fetchone()[0]

        # 待重试数
        pending_retry = self.count_pending_retry_records()

        success_rate = round(success / total * 100, 2) if total > 0 else 0.0
        today_success_rate = round(
            today_success / today_total * 100, 2) if today_total > 0 else 0.0

        return {
            'total_uploads': total,
            'success_count': success,
            'failed_count': failed,
            'success_rate': success_rate,
            'pending_retry_count': pending_retry,
            'today_uploads': today_total,
            'today_success': today_success,
            'today_success_rate': today_success_rate,
        }

    def close(self):
        """
        关闭数据库连接
        在程序退出时调用,释放资源
        """
        if self._connection:
            self._connection.close()
            self._connection = None
            DatabaseManager._instance = None
