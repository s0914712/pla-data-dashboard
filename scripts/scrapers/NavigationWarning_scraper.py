#!/usr/bin/env python3
"""
===============================================================================
中國海事局航行警告爬蟲 (軍演專用版) / MSA Navigation Warning Scraper (Military Focus)
===============================================================================

目標: https://www.msa.gov.cn/page/channelArticles.do?channelids=9C219298-B27F-460E-995A-99401B3FF6AF
用途: 爬取中國海事局發布的軍事演習相關航行警告
作者: s0914712
GitHub: https://github.com/s0914712/pla-data-dashboard
"""

import time
import re
from datetime import datetime
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


class NavigationWarningScraper(BaseScraper):
    """中國海事局航行警告爬蟲（軍演專用）"""
    
    BASE_URL = "https://www.msa.gov.cn"
    # 航行警告總頻道（包含所有海事局）
    NAV_WARNING_CHANNEL = '9C219298-B27F-460E-995A-99401B3FF6AF'
    
    # 🎯 軍事演習關鍵字（必須包含至少一個）
    MILITARY_KEYWORDS = [
        # 核心軍事關鍵字
        '军事', '軍事',
        '演习', '演習',
        '实弹', '實彈',
        '火炮射击', '火炮射擊',
        '射击训练', '射擊訓練',
        '禁航', '禁止驶入', '禁止駛入',
        '火箭发射', '火箭發射',
        '火箭残骸', '火箭殘骸',
        'MILITARY', 'EXERCISE',
        
        # 擴展關鍵字
        '军演', '軍演',
        '演训', '演訓',
        '联合演练', '聯合演練',
        '实战化训练', '實戰化訓練',
        '导弹试射', '導彈試射',
        '武器试验', '武器試驗',
        '海上实弹', '海上實彈',
        '空中演练', '空中演練',
        '战备巡航', '戰備巡航',
        '军事禁区', '軍事禁區',
        '靶场', '靶場',
        '射击场', '射擊場',
    ]
    
    # 排除關鍵字（包含這些的不算軍演）
    EXCLUDE_KEYWORDS = [
        '拖带', '拖帶',
        'LNG', '液化天然气',
        '施工', '海上施工',
        '測量', '测量',
        '打捞', '打撈',
        '载运', '載運',
        '大件', '超大件',
        '加注', '補給',
    ]
    
    # 海事局代碼映射
    MSA_CODE_MAP = {
        '沪': '上海海事局',
        '津': '天津海事局',
        '辽': '辽宁海事局',
        '冀': '河北海事局',
        '鲁': '山东海事局',
        '浙': '浙江海事局',
        '闽': '福建海事局',
        '粤': '广东海事局',
        '桂': '广西海事局',
        '琼': '海南海事局',
        '深': '深圳海事局',
        '厦': '厦门海事局',
        '甬': '宁波海事局',
        '青': '青岛海事局',
        '连': '大连海事局',
        '珠': '珠海海事局',
        '汕': '汕头海事局',
        '湛': '湛江海事局',
        '苏': '江苏海事局',
        '长江': '长江海事局',
    }
    
    def __init__(self, timeout: int = 30, delay: float = 1.0):
        super().__init__(name="msa_military", timeout=timeout, delay=delay)
    
    def is_military_exercise(self, title: str) -> bool:
        """
        判斷是否為軍事演習相關警告
        
        Args:
            title: 標題
            
        Returns:
            是否為軍演相關
        """
        title_lower = title.lower()
        
        # 先檢查排除關鍵字
        for exclude in self.EXCLUDE_KEYWORDS:
            if exclude.lower() in title_lower:
                return False
        
        # 檢查是否包含軍事關鍵字
        for keyword in self.MILITARY_KEYWORDS:
            if keyword.lower() in title_lower:
                return True
        
        return False
    
    def extract_msa_from_title(self, title: str) -> str:
        """從標題中提取海事局名稱"""
        # 方法1: 匹配航警代碼（如：沪航警88/26）
        for code, msa_name in self.MSA_CODE_MAP.items():
            if f'{code}航警' in title or f'{code}航行警告' in title:
                return msa_name
        
        # 方法2: 直接匹配海事局名稱
        for msa_name in self.MSA_CODE_MAP.values():
            if msa_name.replace('海事局', '') in title:
                return msa_name
        
        return '未知海事局'
    
    def extract_matched_keywords(self, title: str) -> List[str]:
        """提取匹配到的軍事關鍵字"""
        matched = []
        title_lower = title.lower()
        
        for keyword in self.MILITARY_KEYWORDS:
            if keyword.lower() in title_lower:
                matched.append(keyword)
        
        return matched
    
    def scrape_page(self, page: int, page_size: int = 50) -> List[Dict]:
        """
        爬取單頁航行警告（只返回軍演相關）
        
        Args:
            page: 頁碼（從1開始）
            page_size: 每頁數量
            
        Returns:
            軍演相關航行警告列表
        """
        url = f"{self.BASE_URL}/page/channelArticles.do"
        params = {
            'channelids': self.NAV_WARNING_CHANNEL,
            'currpage': str(page),
            'pagesize': str(page_size)
        }
        
        html = self.fetch_page(
            url + '?' + '&'.join(f"{k}={v}" for k, v in params.items())
        )
        
        if not html:
            return []
        
        warnings = []
        
        # 解析 HTML
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, 'html.parser')
        all_lis = soup.find_all('li')
        
        for li in all_lis:
            link = li.find('a', href=lambda x: x and 'articleId' in x)
            if not link:
                continue
            
            # 提取標題
            title_span = link.find('span')
            title = title_span.text.strip() if title_span else link.text.strip()
            
            # 🎯 關鍵過濾：只保留軍演相關
            if not self.is_military_exercise(title):
                continue
            
            # 提取日期
            date_text = None
            for span in li.find_all('span'):
                text = span.text.strip()
                if re.match(r'\[\d{4}-\d{2}-\d{2}\]', text):
                    date_text = text.strip('[]')
                    break
            
            # 提取 article ID 和完整 URL
            href = link['href']
            article_id = None
            if 'articleId=' in href:
                article_id = href.split('articleId=')[1].split('&')[0]
            
            full_url = self.BASE_URL + href if not href.startswith('http') else href
            
            # 識別海事局
            msa_name = self.extract_msa_from_title(title)
            
            # 提取匹配的關鍵字
            matched_keywords = self.extract_matched_keywords(title)
            
            warning = {
                'title': title,
                'msa': msa_name,
                'matched_keywords': ','.join(matched_keywords),
                'date': date_text,
                'article_id': article_id,
                'url': full_url,
                'scraped_at': datetime.now().isoformat()
            }
            
            warnings.append(warning)
        
        return warnings
    
    def run(self, max_pages: int = 50, days_back: int = 365) -> List[Dict]:
        """
        執行完整爬取流程
        
        Args:
            max_pages: 最大爬取頁數（預設50頁，確保覆蓋足夠範圍）
            days_back: 爬取過去幾天的數據（預設365天，一年內的軍演）
            
        Returns:
            標準格式的軍演警告列表
        """
        print(f"[{self.name}] 🎯 開始爬取軍事演習相關航行警告...")
        print(f"[{self.name}] 📅 目標: 過去 {days_back} 天，最多 {max_pages} 頁")
        print(f"[{self.name}] 🔍 軍事關鍵字: {len(self.MILITARY_KEYWORDS)} 個")
        
        all_warnings = []
        seen_ids = set()
        
        for page in range(1, max_pages + 1):
            print(f"[{self.name}] 📄 爬取第 {page}/{max_pages} 頁...")
            
            warnings = self.scrape_page(page)
            
            if not warnings:
                print(f"[{self.name}] ⚠️  第 {page} 頁無軍演相關數據")
                # 繼續爬取，不要停止（可能只是這頁沒有）
                if page >= 10:  # 但如果連續10頁都沒有，則停止
                    consecutive_empty = True
                    for check_page in range(max(1, page - 9), page + 1):
                        if check_page in [w.get('_page', 0) for w in all_warnings]:
                            consecutive_empty = False
                            break
                    if consecutive_empty:
                        print(f"[{self.name}] ⚠️  連續多頁無數據，停止爬取")
                        break
                continue
            
            # 去重並過濾日期
            page_added = 0
            for warning in warnings:
                # 檢查重複
                if warning['article_id'] in seen_ids:
                    continue
                
                # 檢查日期範圍
                date_obj = self.parse_date(warning['date'])
                if not date_obj or not self.is_within_days(date_obj, days_back):
                    continue
                
                seen_ids.add(warning['article_id'])
                warning['_page'] = page  # 記錄頁碼（內部使用）
                all_warnings.append(warning)
                page_added += 1
            
            print(f"[{self.name}] ✅ 本頁新增 {page_added} 條軍演警告，累計 {len(all_warnings)} 條")
        
        print(f"\n[{self.name}] ✅ 爬取完成！共 {len(all_warnings)} 條軍事演習警告")
        
        # 移除內部字段
        for warning in all_warnings:
            warning.pop('_page', None)
        
        # 轉換為標準格式
        return self.to_standard_format(all_warnings)
    
    def to_standard_format(self, warnings: List[Dict]) -> List[Dict]:
        """
        轉換為標準格式
        
        標準格式:
        {
            'date': str (YYYY-MM-DD),
            'title': str,
            'msa': str (海事局名稱),
            'matched_keywords': str (匹配的關鍵字，逗號分隔),
            'article_id': str,
            'url': str,
            'scraped_at': str (ISO格式時間戳)
        }
        """
        standardized = []
        for warning in warnings:
            date_obj = self.parse_date(warning.get('date', ''))
            std_warning = {
                'date': date_obj.strftime('%Y-%m-%d') if date_obj else '',
                'title': warning.get('title', '').strip(),
                'msa': warning.get('msa', ''),
                'matched_keywords': warning.get('matched_keywords', ''),
                'article_id': warning.get('article_id', ''),
                'url': warning.get('url', ''),
                'scraped_at': warning.get('scraped_at', '')
            }
            if std_warning['date'] and std_warning['title']:
                standardized.append(std_warning)
        
        return standardized


def test_scraper():
    """測試爬蟲"""
    print("=" * 80)
    print("MSA Military Exercise Warning Scraper 測試")
    print("=" * 80)
    
    with NavigationWarningScraper(delay=1.0) as scraper:
        warnings = scraper.run(max_pages=10, days_back=180)
        
        print(f"\n總計爬取: {len(warnings)} 條軍事演習警告\n")
        
        if not warnings:
            print("⚠️  未找到軍事演習相關警告")
            return
        
        # 按海事局統計
        from collections import Counter
        msa_counts = Counter(w['msa'] for w in warnings)
        
        print("海事局統計:")
        for msa, count in msa_counts.most_common():
            print(f"  {msa}: {count} 條")
        
        # 按關鍵字統計
        all_keywords = []
        for w in warnings:
            if w['matched_keywords']:
                all_keywords.extend(w['matched_keywords'].split(','))
        
        keyword_counts = Counter(all_keywords)
        print("\n關鍵字統計 (Top 10):")
        for keyword, count in keyword_counts.most_common(10):
            print(f"  {keyword}: {count} 次")
        
        print("\n最新10條警告:")
        sorted_warnings = sorted(warnings, key=lambda x: x['date'], reverse=True)
        for i, warning in enumerate(sorted_warnings[:10], 1):
            print(f"\n{i}. [{warning['date']}] {warning['title']}")
            print(f"   海事局: {warning['msa']}")
            print(f"   關鍵字: {warning['matched_keywords']}")
            print(f"   URL: {warning['url']}")


if __name__ == '__main__':
    test_scraper()
