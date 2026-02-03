#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
中國海事局航行警告爬蟲 / MSA Navigation Warning Scraper
===============================================================================

專注於軍事任務、實彈射擊等相關公告
"""

import re
from datetime import datetime
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


class NavigationWarningScraper(BaseScraper):
    """中國海事局航行警告爬蟲（軍事專用）"""
    
    BASE_URL = "https://www.msa.gov.cn"
    
    # 各海事局頻道 ID
    CHANNELS = {
        '上海海事局': '94DF14CE-1110-415D-A44E-67593E76619F',
        '天津海事局': 'BDBA5FAD-6E5D-4867-9F97-0FCF8EFB8636',
        '辽宁海事局': 'C8896863-B101-4C43-8705-536A03EB46FF',
        '河北海事局': '93B73989-D220-45F9-BC32-70A6EBA35180',
        '山东海事局': '36EA3354-C8F8-4953-ABA0-82D6D989C750',
        '浙江海事局': '8E10EA74-EB9E-4C96-90F8-F891968ADD80',
        '福建海事局': '7B084057-6038-4570-A0FB-44E9204C4B1D',
        '广东海事局': '1E478D40-9E85-4918-BF12-478B8A19F4A8',
        '广西海事局': '86DE2FFF-FF2C-47F9-8359-FD1F20D6508F',
        '海南海事局': 'D3340711-057B-494B-8FA0-9EEDC4C5EAD9',
        '深圳海事局': '325FDC08-92B4-4313-A63E-E5C165BE98EC',
        '连云港海事局': 'FA4501F3-DBE4-4F70-BC72-6F27132D4E04',
    }
    
    # 軍事相關關鍵字
    MILITARY_KEYWORDS = [
        '军事', '演习', '实弹', '火炮射击', '射击训练',
        '禁航', '禁止驶入', 'MILITARY', '火箭发射', '火箭残骸',
        '軍事', '演習', '實彈', '射擊訓練', '禁止駛入',
        'EXERCISE', 'MISSION', '军演', '軍演'
    ]
    
    def __init__(self, timeout: int = 30, delay: float = 1.0):
        super().__init__(name="msa_military", timeout=timeout, delay=delay)
    
    def is_military_related(self, title: str) -> bool:
        """檢查標題是否與軍事相關"""
        return any(kw in title for kw in self.MILITARY_KEYWORDS)
    
    def fetch_channel_list(self, channel_id: str, channel_name: str, page: int = 1) -> List[Dict]:
        """取得特定海事局的航警列表"""
        url = f"{self.BASE_URL}/page/channelArticles.do?channelids={channel_id}&pageNo={page}"
        
        html = self.fetch_page(url)
        if not html:
            print(f"[{self.name}] ❌ 無法訪問 {channel_name}")
            return []
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        articles = []
        
        # 尋找所有文章鏈接
        for link in soup.find_all('a', href=True):
            href = link['href']
            if '/page/article.do?articleId=' not in href:
                continue
            
            title = link.get_text(strip=True)
            if not title:
                continue
            
            # 檢查是否為軍事相關
            is_military = self.is_military_related(title)
            
            # 提取日期
            date_text = ''
            next_sibling = link.find_next_sibling(string=True)
            if next_sibling:
                date_match = re.search(r'\d{4}-\d{2}-\d{2}', str(next_sibling))
                if date_match:
                    date_text = date_match.group()
            
            if not date_text:
                parent = link.find_parent('li') or link.find_parent('div')
                if parent:
                    parent_text = parent.get_text()
                    date_match = re.search(r'\d{4}-\d{2}-\d{2}', parent_text)
                    if date_match:
                        date_text = date_match.group()
            
            full_url = href if href.startswith('http') else self.BASE_URL + href
            
            articles.append({
                'title': title,
                'url': full_url,
                'date': date_text,
                'channel': channel_name,
                'is_military': is_military
            })
        
        return articles
    
    def fetch_article_content(self, url: str) -> Optional[str]:
        """取得公告詳細內容並清理"""
        html = self.fetch_page(url)
        if not html:
            return None
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # 嘗試多種內容選擇器
        content = None
        for selector in ['div.article-content', 'div.content', 'div.TRS_Editor',
                        'div.detail-content', 'article', 'div.main-content']:
            content_div = soup.select_one(selector)
            if content_div:
                content = content_div.get_text(strip=True)
                break
        
        # 如果找不到，取得 body 內的主要文字
        if not content:
            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer']):
                    tag.decompose()
                content = body.get_text(separator=' ', strip=True)
        
        # 提取核心內容
        if content:
            content = self.extract_core_content(content)
        
        return content
    
    def extract_core_content(self, text: str) -> str:
        """提取核心內容：從航警編號到「收藏」之間的文字"""
        # 尋找航警編號開始位置
        start_patterns = [
            r'([a-zA-Z沪津辽冀鲁浙闽粤桂琼深厦甬青连珠汕湛苏]航警?\d+/\d+)',
            r'([A-Z]{2,3}\d+/\d+)',
        ]
        
        start_pos = -1
        
        for pattern in start_patterns:
            matches = list(re.finditer(pattern, text))
            if matches:
                for match in matches:
                    after_text = text[match.end():match.end()+50]
                    if '，' in after_text or '。' in after_text or ',' in after_text:
                        start_pos = match.start()
                        break
                if start_pos != -1:
                    break
        
        if start_pos == -1:
            return text[:500]
        
        # 尋找結束位置
        end_patterns = ['收藏', '打印本页', '关闭窗口']
        end_pos = len(text)
        
        for end_pattern in end_patterns:
            pos = text.find(end_pattern, start_pos)
            if pos != -1 and pos < end_pos:
                end_pos = pos
        
        # 提取並清理
        core_content = text[start_pos:end_pos].strip()
        core_content = re.sub(r'\s+', ' ', core_content)
        
        # 限制長度
        if len(core_content) > 1000:
            core_content = core_content[:1000] + '...'
        
        return core_content
    
    def parse_coordinates(self, text: str) -> List[Dict]:
        """解析經緯度座標"""
        if not text:
            return []
        
        coords = []
        
        # 格式1: 38-31.3N121-33.2E
        pattern1 = r'(\d{1,2})-(\d{1,2}(?:\.\d+)?)\s*([NS])\s*(\d{1,3})-(\d{1,2}(?:\.\d+)?)\s*([EW])'
        for match in re.finditer(pattern1, text):
            lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir = match.groups()
            lat = float(lat_deg) + float(lat_min) / 60
            lon = float(lon_deg) + float(lon_min) / 60
            if lat_dir == 'S':
                lat = -lat
            if lon_dir == 'W':
                lon = -lon
            coords.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'raw': match.group()
            })
        
        # 格式2: N 39°24′35″、E 119°13′44″
        pattern2 = r'([NS])?\s*(\d{1,2})°(\d{1,2})′(\d{1,2}(?:\.\d+)?)?″?\s*([NS])?\s*[、,\s]*([EW])?\s*(\d{1,3})°(\d{1,2})′(\d{1,2}(?:\.\d+)?)?″?\s*([EW])?'
        for match in re.finditer(pattern2, text):
            groups = match.groups()
            lat_dir = groups[0] or groups[4] or 'N'
            lat_deg, lat_min, lat_sec = groups[1], groups[2], groups[3] or '0'
            lon_dir = groups[5] or groups[9] or 'E'
            lon_deg, lon_min, lon_sec = groups[6], groups[7], groups[8] or '0'
            
            lat = float(lat_deg) + float(lat_min) / 60 + float(lat_sec) / 3600
            lon = float(lon_deg) + float(lon_min) / 60 + float(lon_sec) / 3600
            
            if lat_dir == 'S':
                lat = -lat
            if lon_dir == 'W':
                lon = -lon
            
            coords.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'raw': match.group()
            })
        
        # 格式3: 38.5N 121.5E
        pattern3 = r'(\d{1,2}(?:\.\d+)?)\s*([NS])\s*(\d{1,3}(?:\.\d+)?)\s*([EW])'
        for match in re.finditer(pattern3, text):
            lat, lat_dir, lon, lon_dir = match.groups()
            lat = float(lat)
            lon = float(lon)
            if lat_dir == 'S':
                lat = -lat
            if lon_dir == 'W':
                lon = -lon
            coords.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'raw': match.group()
            })
        
        # 去重
        seen = set()
        unique_coords = []
        for c in coords:
            key = (c['lat'], c['lon'])
            if key not in seen:
                seen.add(key)
                unique_coords.append(c)
        
        return unique_coords
    
    def parse_time_period(self, text: str) -> List[str]:
        """解析時間範圍"""
        if not text:
            return []
        
        times = []
        
        # 格式1: X月X日X时至X日X时
        pattern1 = r'(\d{1,2})月(\d{1,2})日(\d{1,2})时至(\d{1,2})日?(\d{1,2})时'
        times.extend([m.group() for m in re.finditer(pattern1, text)])
        
        # 格式2: 自X月X日X时至X月X日X时
        pattern2 = r'自?(\d{1,2})月(\d{1,2})日(\d{1,4})时至(\d{1,2})月?(\d{1,2})日?(\d{1,4})时'
        times.extend([m.group() for m in re.finditer(pattern2, text)])
        
        # 格式3: XXXX年X月X日
        pattern3 = r'(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):?(\d{2}))?(?:时)?(?:至|[-~])(\d{4})?年?(\d{1,2})?月?(\d{1,2})日?(?:\s*(\d{1,2}):?(\d{2}))?(?:时)?'
        times.extend([m.group() for m in re.finditer(pattern3, text)])
        
        # 格式4: X日XXXX时至XXXX时
        pattern4 = r'(\d{1,2})日(\d{4})时至(\d{4})时'
        times.extend([m.group() for m in re.finditer(pattern4, text)])
        
        return list(set(times))
    
    def run(self, days_back: int = 365, max_pages: int = 1) -> List[Dict]:
        """
        執行爬蟲 (符合 BaseScraper 的簽名)
        
        Args:
            days_back: 追溯天數
            max_pages: 每個海事局最多抓取頁數（預設1頁=20篇）
            
        Returns:
            標準格式的航行警告列表
        """
        print(f"[{self.name}] 🚢 開始爬取 {len(self.CHANNELS)} 個海事局的航行警告...")
        
        all_warnings = []
        military_only = True  # 固定只抓軍事相關
        max_articles_per_channel = max_pages * 20  # 每頁約20篇
        
        for channel_name, channel_id in self.CHANNELS.items():
            print(f"[{self.name}] 📍 正在處理: {channel_name}")
            
            # 取得列表
            articles = self.fetch_channel_list(channel_id, channel_name)
            
            if military_only:
                articles = [a for a in articles if a['is_military']]
            
            # 日期過濾
            filtered_articles = []
            for article in articles[:max_articles_per_channel]:
                if article['date']:
                    date_obj = self.parse_date(article['date'])
                    if date_obj and self.is_within_days(date_obj, days_back):
                        filtered_articles.append(article)
                else:
                    filtered_articles.append(article)
            
            articles = filtered_articles
            
            print(f"[{self.name}]    找到 {len(articles)} 篇軍事相關公告")
            
            for article in articles:
                # 取得詳細內容
                content = self.fetch_article_content(article['url'])
                
                if content:
                    # 解析座標
                    coordinates = self.parse_coordinates(content)
                    
                    # 解析時間
                    time_periods = self.parse_time_period(content)
                    
                    warning = {
                        'title': article['title'],
                        'channel': channel_name,
                        'publish_date': article['date'],
                        'url': article['url'],
                        'coordinates': coordinates,
                        'coordinate_count': len(coordinates),
                        'time_periods': time_periods,
                        'content_preview': content,
                        'is_military': article['is_military'],
                        'scraped_at': datetime.now().isoformat()
                    }
                    
                    all_warnings.append(warning)
                
                import time
                time.sleep(0.5)
            
            import time
            time.sleep(1)
        
        print(f"[{self.name}] ✅ 完成！共抓取 {len(all_warnings)} 篇航行警告")
        
        return self.to_standard_format(all_warnings)
    
    def to_standard_format(self, warnings: List[Dict]) -> List[Dict]:
        """轉換為標準格式"""
        standardized = []
        
        for w in warnings:
            # 格式化經緯度
            coords_str = ''
            coords_raw = ''
            if w['coordinates']:
                coords_list = [f"{c['lat']},{c['lon']}" for c in w['coordinates']]
                coords_str = '; '.join(coords_list)
                coords_raw_list = [c['raw'] for c in w['coordinates']]
                coords_raw = '; '.join(coords_raw_list)
            
            # 格式化時間範圍
            time_periods_str = '; '.join(w['time_periods']) if w['time_periods'] else ''
            
            std_warning = {
                'publish_date': w['publish_date'],
                'title': w['title'],
                'channel': w['channel'],
                'time_periods': time_periods_str,
                'coordinate_count': w['coordinate_count'],
                'coordinates': coords_str,
                'coordinates_raw': coords_raw,
                'content_preview': w['content_preview'],
                'url': w['url'],
                'scraped_at': w['scraped_at']
            }
            
            standardized.append(std_warning)
        
        return standardized
