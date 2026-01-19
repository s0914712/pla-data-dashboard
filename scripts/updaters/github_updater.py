#!/usr/bin/env python3
"""
===============================================================================
GitHub 更新器 / GitHub Updater
===============================================================================

將更新後的 CSV 推送到 GitHub
支援 GitHub Actions 環境和本地環境
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional


class GitHubUpdater:
    """GitHub 推送工具"""
    
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
    
    def configure_git(self, name: str = "GitHub Action", email: str = "action@github.com"):
        """配置 Git 用戶信息"""
        self._run_git('config', '--local', 'user.email', email)
        self._run_git('config', '--local', 'user.name', name)
    
    def has_changes(self, file_path: str) -> bool:
        """檢查文件是否有變更"""
        success, output = self._run_git('status', '--porcelain', file_path)
        return bool(output) if success else False
    
    def commit_and_push(
        self, 
        file_paths: list, 
        message: Optional[str] = None,
        branch: str = 'main'
    ) -> bool:
        """
        提交並推送變更
        
        Args:
            file_paths: 要提交的文件路徑列表
            message: 提交訊息（預設自動生成）
            branch: 目標分支
            
        Returns:
            是否成功
        """
        if message is None:
            message = f"Auto-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        # 添加文件
        for file_path in file_paths:
            success, _ = self._run_git('add', file_path)
            if not success:
                print(f"[GitHubUpdater] Failed to add {file_path}")
                return False
        
        # 檢查是否有變更
        success, output = self._run_git('diff', '--staged', '--quiet')
        if success:
            print("[GitHubUpdater] No changes to commit")
            return True
        
        # 提交
        success, output = self._run_git('commit', '-m', message)
        if not success:
            print(f"[GitHubUpdater] Commit failed: {output}")
            return False
        
        print(f"[GitHubUpdater] Committed: {message}")
        
        # 推送
        success, output = self._run_git('push', 'origin', branch)
        if not success:
            print(f"[GitHubUpdater] Push failed: {output}")
            return False
        
        print(f"[GitHubUpdater] Pushed to {branch}")
        return True
    
    def create_run_log(self, log_data: dict, log_dir: str = 'data/logs') -> str:
        """
        創建執行日誌
        
        Args:
            log_data: 日誌數據
            log_dir: 日誌目錄
            
        Returns:
            日誌文件路徑
        """
        import json
        
        log_path = Path(self.repo_path) / log_dir
        log_path.mkdir(parents=True, exist_ok=True)
        
        filename = f"update_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        log_file = log_path / filename
        
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        
        return str(log_file)


def test_github_updater():
    """測試 GitHub 更新器"""
    updater = GitHubUpdater('.')
    
    # 測試配置
    updater.configure_git()
    
    # 測試日誌創建
    log_data = {
        'timestamp': datetime.now().isoformat(),
        'status': 'test',
        'steps': []
    }
    
    # log_file = updater.create_run_log(log_data, '/tmp/logs')
    # print(f"Created log: {log_file}")


if __name__ == '__main__':
    test_github_updater()
