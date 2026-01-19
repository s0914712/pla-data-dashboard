#!/usr/bin/env python3
"""
===============================================================================
GitHub 更新器 / GitHub Updater
===============================================================================

將更新後的數據文件（CSV/JSON）推送到 GitHub
專門用於 GitHub Actions 自動化工作流程
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List


class GitHubUpdater:
    """GitHub 數據推送工具（僅用於數據文件，不上傳程式碼）"""
    
    def __init__(self, repo_path: str = '.', token: Optional[str] = None):
        """
        初始化
        
        Args:
            repo_path: Git 倉庫路徑
            token: GitHub Token（可選，優先使用環境變數）
        """
        self.repo_path = Path(repo_path).resolve()
        self.token = token or os.environ.get('GITHUB_TOKEN', '')
    
    def _run_git(self, *args) -> tuple:
        """執行 Git 命令"""
        try:
            result = subprocess.run(
                ['git'] + list(args),
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return True, result.stdout.strip()
        except subprocess.CalledProcessError as e:
            return False, e.stderr.strip()
    
    def configure_git(self, name: str = "GitHub Action Bot", email: str = "action@github.com"):
        """配置 Git 用戶信息"""
        self._run_git('config', '--local', 'user.email', email)
        self._run_git('config', '--local', 'user.name', name)
        print(f"[GitHubUpdater] Git configured: {name} <{email}>")
    
    def has_changes(self, file_path: str) -> bool:
        """檢查指定文件是否有變更"""
        success, output = self._run_git('status', '--porcelain', file_path)
        return bool(output) if success else False
    
    def commit_and_push_data(
        self, 
        data_files: List[str], 
        message: Optional[str] = None,
        branch: str = 'main'
    ) -> bool:
        """
        提交並推送數據文件變更
        
        Args:
            data_files: 數據文件路徑列表（如 ['data/news.csv', 'data/stats.json']）
            message: 提交訊息（預設自動生成）
            branch: 目標分支
            
        Returns:
            是否成功
        """
        if not data_files:
            print("[GitHubUpdater] No data files specified")
            return False
        
        # 預設提交訊息
        if message is None:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            message = f"🤖 Auto-update data: {timestamp}"
        
        # 添加數據文件
        added_files = []
        for file_path in data_files:
            if not Path(file_path).exists():
                print(f"[GitHubUpdater] ⚠️  File not found: {file_path}")
                continue
            
            success, _ = self._run_git('add', file_path)
            if success:
                added_files.append(file_path)
                print(f"[GitHubUpdater] ✓ Staged: {file_path}")
            else:
                print(f"[GitHubUpdater] ✗ Failed to stage: {file_path}")
        
        if not added_files:
            print("[GitHubUpdater] No files were staged")
            return False
        
        # 檢查是否有實際變更
        success, _ = self._run_git('diff', '--staged', '--quiet')
        if success:
            print("[GitHubUpdater] No changes detected in staged files")
            return True
        
        # 顯示變更摘要
        success, diff_stat = self._run_git('diff', '--staged', '--stat')
        if success and diff_stat:
            print(f"[GitHubUpdater] Changes:\n{diff_stat}")
        
        # 提交
        success, output = self._run_git('commit', '-m', message)
        if not success:
            print(f"[GitHubUpdater] ✗ Commit failed: {output}")
            return False
        
        print(f"[GitHubUpdater] ✓ Committed: {message}")
        
        # 推送到遠端
        success, output = self._run_git('push', 'origin', branch)
        if not success:
            print(f"[GitHubUpdater] ✗ Push failed: {output}")
            return False
        
        print(f"[GitHubUpdater] ✓ Pushed to origin/{branch}")
        return True
    
    def get_last_update_time(self, file_path: str) -> Optional[datetime]:
        """
        獲取文件最後更新時間（來自 Git）
        
        Args:
            file_path: 文件路徑
            
        Returns:
            最後提交時間
        """
        success, output = self._run_git('log', '-1', '--format=%ai', '--', file_path)
        if success and output:
            try:
                return datetime.strptime(output[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                pass
        return None
    
    def create_summary_log(self, stats: dict, log_file: str = 'data/last_update.json') -> bool:
        """
        創建更新摘要日誌
        
        Args:
            stats: 統計數據
            log_file: 日誌文件路徑
            
        Returns:
            是否成功
        """
        import json
        
        log_data = {
            'last_update': datetime.now().isoformat(),
            'stats': stats
        }
        
        try:
            log_path = Path(self.repo_path) / log_file
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            
            print(f"[GitHubUpdater] ✓ Created summary log: {log_file}")
            return True
        except Exception as e:
            print(f"[GitHubUpdater] ✗ Failed to create log: {e}")
            return False


def test_github_updater():
    """測試 GitHub 更新器"""
    updater = GitHubUpdater('.')
    
    # 測試配置
    updater.configure_git()
    
    # 測試摘要日誌
    stats = {
        'news_scraped': 42,
        'classified': 35,
        'relevant': 12
    }
    updater.create_summary_log(stats)
    
    # 測試推送（注意：這會實際執行 Git 操作）
    # updater.commit_and_push_data(
    #     data_files=['data/last_update.json'],
    #     message="Test update"
    # )


if __name__ == '__main__':
    test_github_updater()
