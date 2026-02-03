#!/usr/bin/env python3
"""
===============================================================================
中國海事局航行警告爬蟲 (軍演專用版 - 含詳細內容)
MSA Navigation Warning Scraper (Military Focus - Full Content)
===============================================================================

目標: 爬取軍事演習相關航行警告的詳細信息
包含: 經緯度、日期範圍、警告概要
"""

import time
import re
from datetime import datetime
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


class NavigationWarningScraper(BaseScraper):
    """中國海事局航行警告爬蟲（軍演專用 - 含詳細內容）"""
    
    BASE_URL = "https://www.msa.gov.cn"
    NAV_WARNING_CHANNEL = '9C219298-B27F-460E-995A-99401B3FF6AF'
    
    # 🎯 軍事演習關鍵字
    MILITARY_KEYWORDS = [
        '军事', '軍事', '演习', '演習', '实弹', '實彈',
        '火炮射击', '火炮射擊', '射击训练', '射擊訓練',
        '禁航', '禁止驶入', '禁止駛入',
        '火箭发射', '火箭發射', '火箭残骸', '火箭殘骸',
        'MILITARY', 'EXERCISE',
        '军演', '軍演', '演训', '演訓',
        '联合演练', '聯合演練', '实战化训练', '實戰化訓練',
        '导弹试射', '導彈試射', '武器试验', '武器試驗',
        '海上实弹', '海上實彈', '空中演练', '空中演練',
        '战备巡航', '戰備巡航', '军事禁区', '軍事禁區',
        '靶场', '靶場', '射击场', '射擊場',
    ]
    
    # 排除關鍵字
    EXCLUDE_KEYWORDS = [
        '拖带', '拖帶', 'LNG', '液化天然气',
        '施工', '海上施工', '測量', '测量',
        '打捞', '打撈', '载运', '載運',
        '大件', '超大件', '加注', '補給',
    ]
    
    # 海事局代碼映射
    MSA_CODE_MAP = {
        '沪': '上海海事局', '津': '天津海事局',
        '辽': '辽宁海事局', '冀': '河北海事局',
        '鲁': '山东海事局', '浙': '浙江海事局',
        '闽': '福建海事局', '粤': '广东海事局',
        '桂': '广西海事局', '琼': '海南海事局',
        '深': '深圳海事局', '厦': '厦门海事局',
        '甬': '宁波海事局', '青': '青岛海事局',
        '连': '大连海事局', '珠': '珠海海事局',
        '汕': '汕头海事局', '湛': '湛江海事局',
        '苏': '江苏海事局', '长江': '长江海事局',
    }
    
    def __init__(self, timeout: int = 30, delay: float = 1.0):
        super().__init__(name="msa_military", timeout=timeout, delay=delay)
    
    def is_military_exercise(self, title: str) -> bool:
        """判斷是否為軍事演習相關警告"""
        title_lower = title.lower()
        
        # 檢查排除關鍵字
        for exclude in self.EXCLUDE_KEYWORDS:
            if exclude.lower() in title_lower:
                return False
        
        # 檢查軍事關鍵字
        for keyword in self.MILITARY_KEYWORDS:
            if keyword.lower() in title_lower:
                return True
        
        return False
    
    def extract_msa_from_title(self, title: str) -> str:
        """從標題中提取海事局名稱"""
        for code, msa_name in self.MSA_CODE_MAP.items():
            if f'{code}航警' in title or f'{code}航行警告' in title:
                return msa_name
        
        for msa_name in self.MSA_CODE_MAP.values():
            if msa_name.replace('海事局', '') in title:
                return msa_name
        
        return '未知海事局'
    
    def extract_matched_keywords(self, title: str) -> List[str]:
        """提取匹配的軍事關鍵字"""
        matched = []
        title_lower = title.lower()
        
        for keyword in self.MILITARY_KEYWORDS:
            if keyword.lower() in title_lower:
                matched.append(keyword)
        
        return matched
    
    def parse_article_detail(self, url: str) -> Dict:
        """
        解析文章詳細內容，提取經緯度、日期範圍、概要
        
        Returns:
            {
                'coordinates': List[Dict],  # 經緯度列表
                'date_range': str,          # 日期範圍
                'summary': str              # 概要
            }
        """
        html = self.fetch_page(url)
        if not html:
            return {
                'coordinates': [],
                'date_range': '',
                'summary': ''
            }
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # 提取正文內容
        content_div = soup.find('div', class_='content') or soup.find('div', id='content')
        if not content_div:
            # 嘗試其他可能的內容區域
            content_div = soup.find('div', class_='article-content') or soup.find('article')
        
        if not content_div:
            return {'coordinates': [], 'date_range': '', 'summary': ''}
        
        text = content_div.get_text()
        
        # 🔍 提取經緯度
        coordinates = self.extract_coordinates(text)
        
        # 🔍 提取日期範圍
        date_range = self.extract_date_range(text)
        
        # 🔍 提取概要（前300字）
        summary = self.extract_summary(text)
        
        return {
            'coordinates': coordinates,
            'date_range': date_range,
            'summary': summary
        }
    
    def extract_coordinates(self, text: str) -> List[Dict]:
        """
        提取經緯度坐標
        
        支持格式:
        - 30°15′N 122°30′E
        - 30-15N 122-30E
        - 30°15.5′N 122°30.5′E (含小數)
        """
        coordinates = []
        
        # 模式1: 度分秒格式 (30°15′N 122°30′E)
        pattern1 = r'(\d+)°(\d+)(?:′|分)([NS北南])\s+(\d+)°(\d+)(?:′|分)([EW東西])'
        matches1 = re.findall(pattern1, text)
        
        for match in matches1:
            lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir = match
            
            # 轉換為十進制
            lat = float(lat_deg) + float(lat_min) / 60
            lon = float(lon_deg) + float(lon_min) / 60
            
            if lat_dir in ['S', '南']:
                lat = -lat
            if lon_dir in ['W', '西']:
                lon = -lon
            
            coordinates.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'original': f"{lat_deg}°{lat_min}′{lat_dir} {lon_deg}°{lon_min}′{lon_dir}"
            })
        
        # 模式2: 簡化格式 (30-15N 122-30E)
        pattern2 = r'(\d+)-(\d+)([NS北南])\s+(\d+)-(\d+)([EW東西])'
        matches2 = re.findall(pattern2, text)
        
        for match in matches2:
            lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir = match
            
            lat = float(lat_deg) + float(lat_min) / 60
            lon = float(lon_deg) + float(lon_min) / 60
            
            if lat_dir in ['S', '南']:
                lat = -lat
            if lon_dir in ['W', '西']:
                lon = -lon
            
            coordinates.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'original': f"{lat_deg}-{lat_min}{lat_dir} {lon_deg}-{lon_min}{lon_dir}"
            })
        
        # 模式3: 度分秒.小數格式 (30°15.5′N)
        pattern3 = r'(\d+)°([\d.]+)(?:′|分)([NS北南])\s+(\d+)°([\d.]+)(?:′|分)([EW東西])'
        matches3 = re.findall(pattern3, text)
        
        for match in matches3:
            lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir = match
            
            lat = float(lat_deg) + float(lat_min) / 60
            lon = float(lon_deg) + float(lon_min) / 60
            
            if lat_dir in ['S', '南']:
                lat = -lat
            if lon_dir in ['W', '西']:
                lon = -lon
            
            coordinates.append({
                'lat': round(lat, 4),
                'lon': round(lon, 4),
                'original': f"{lat_deg}°{lat_min}′{lat_dir} {lon_deg}°{lon_min}′{lon_dir}"
            })
        
        # 去重
        seen = set()
        unique_coords = []
        for coord in coordinates:
            key = (coord['lat'], coord['lon'])
            if key not in seen:
                seen.add(key)
                unique_coords.append(coord)
        
        return unique_coords
    
    def extract_date_range(self, text: str) -> str:
        """
        提取日期範圍
        
        支持格式:
        - 2024年1月15日至1月20日
        - 1月15日至20日
        - 2024-01-15至2024-01-20
        """
        # 模式1: 完整日期範圍
        pattern1 = r'(\d{4})年(\d{1,2})月(\d{1,2})日至(\d{1,2})月(\d{1,2})日'
        match1 = re.search(pattern1, text)
        if match1:
            year, m1, d1, m2, d2 = match1.groups()
            return f"{year}-{m1.zfill(2)}-{d1.zfill(2)} 至 {year}-{m2.zfill(2)}-{d2.zfill(2)}"
        
        # 模式2: 同月日期範圍
        pattern2 = r'(\d{4})年(\d{1,2})月(\d{1,2})日至(\d{1,2})日'
        match2 = re.search(pattern2, text)
        if match2:
            year, month, d1, d2 = match2.groups()
            return f"{year}-{month.zfill(2)}-{d1.zfill(2)} 至 {year}-{month.zfill(2)}-{d2.zfill(2)}"
        
        # 模式3: 短格式
        pattern3 = r'(\d{1,2})月(\d{1,2})日至(\d{1,2})日'
        match3 = re.search(pattern3, text)
        if match3:
            month, d1, d2 = match3.groups()
            year = datetime.now().year
            return f"{year}-{month.zfill(2)}-{d1.zfill(2)} 至 {year}-{month.zfill(2)}-{d2.zfill(2)}"
        
        # 模式4: ISO 格式
        pattern4 = r'(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})'
        match4 = re.search(pattern4, text)
        if match4:
            return f"{match4.group(1)} 至 {match4.group(2)}"
        
        return ''
    
    def extract_summary(self, text: str) -> str:
        """提取概要（清理後的前300字）"""
        # 移除多餘空白和換行
        summary = re.sub(r'\s+', ' ', text)
        
        # 移除常見的網頁元素
        summary = re.sub(r'(首页|返回|打印|分享|相关链接)', '', summary)
        
        # 截取前300字
        summary = summary.strip()[:300]
        
        return summary
    
    def scrape_page(self, page: int, page_size: int = 50) -> List[Dict]:
        """爬取單頁航行警告（只返回軍演相關）"""
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
            
            # 🎯 只保留軍演相關
            if not self.is_military_exercise(title):
                continue
            
            # 提取日期
            date_text = None
            for span in li.find_all('span'):
                text = span.text.strip()
                if re.match(r'\[\d{4}-\d{2}-\d{2}\]', text):
                    date_text = text.strip('[]')
                    break
            
            # 提取 URL
            href = link['href']
            article_id = None
            if 'articleId=' in href:
                article_id = href.split('articleId=')[1].split('&')[0]
            
            full_url = self.BASE_URL + href if not href.startswith('http') else href
            
            # 識別海事局和關鍵字
            msa_name = self.extract_msa_from_title(title)
            matched_keywords = self.extract_matched_keywords(title)
            
            warning = {
                'title': title,
                'msa': msa_name,
                'matched_keywords': ','.join(matched_keywords),
                'publish_date': date_text,
                'article_id': article_id,
                'url': full_url,
                'scraped_at': datetime.now().isoformat()
            }
            
            warnings.append(warning)
        
        return warnings
    
    def run(self, max_pages: int = 50, days_back: int = 365, fetch_details: bool = True) -> List[Dict]:
        """
        執行完整爬取流程
        
        Args:
            max_pages: 最大爬取頁數
            days_back: 爬取過去幾天的數據
            fetch_details: 是否爬取詳細內容（經緯度、日期範圍、概要）
            
        Returns:
            標準格式的軍演警告列表
        """
        print(f"[{self.name}] 🎯 開始爬取軍事演習相關航行警告...")
        print(f"[{self.name}] 📅 目標: 過去 {days_back} 天，最多 {max_pages} 頁")
        print(f"[{self.name}] 🔍 詳細內容: {'是' if fetch_details else '否'}")
        
        all_warnings = []
        seen_ids = set()
        
        # 第一階段：爬取列表
        for page in range(1, max_pages + 1):
            print(f"[{self.name}] 📄 爬取第 {page}/{max_pages} 頁...")
            
            warnings = self.scrape_page(page)
            
            if not warnings:
                if page >= 10:
                    consecutive_empty = sum(1 for p in range(max(1, page - 9), page + 1) 
                                          if not any(w.get('_page') == p for w in all_warnings))
                    if consecutive_empty >= 10:
                        print(f"[{self.name}] ⚠️  連續多頁無數據，停止爬取")
                        break
                continue
            
            page_added = 0
            for warning in warnings:
                if warning['article_id'] in seen_ids:
                    continue
                
                date_obj = self.parse_date(warning['publish_date'])
                if not date_obj or not self.is_within_days(date_obj, days_back):
                    continue
                
                seen_ids.add(warning['article_id'])
                warning['_page'] = page
                all_warnings.append(warning)
                page_added += 1
            
            print(f"[{self.name}] ✅ 本頁新增 {page_added} 條，累計 {len(all_warnings)} 條")
        
        # 第二階段：爬取詳細內容
        if fetch_details and all_warnings:
            print(f"\n[{self.name}] 📥 開始爬取 {len(all_warnings)} 條警告的詳細內容...")
            
            for i, warning in enumerate(all_warnings, 1):
                print(f"[{self.name}] [{i}/{len(all_warnings)}] {warning['title'][:40]}...")
                
                details = self.parse_article_detail(warning['url'])
                warning.update(details)
                
                # 顯示提取結果
                if details['coordinates']:
                    print(f"[{self.name}]   ✓ 經緯度: {len(details['coordinates'])} 個點")
                if details['date_range']:
                    print(f"[{self.name}]   ✓ 日期範圍: {details['date_range']}")
        
        # 移除內部字段
        for warning in all_warnings:
            warning.pop('_page', None)
        
        print(f"\n[{self.name}] ✅ 爬取完成！共 {len(all_warnings)} 條軍事演習警告")
        
        return self.to_standard_format(all_warnings)
    
    def to_standard_format(self, warnings: List[Dict]) -> List[Dict]:
        """轉換為標準格式"""
        standardized = []
        for warning in warnings:
            date_obj = self.parse_date(warning.get('publish_date', ''))
            
            # 格式化經緯度為字符串
            coords_str = ''
            if warning.get('coordinates'):
                coords_list = [f"{c['lat']},{c['lon']}" for c in warning['coordinates']]
                coords_str = '; '.join(coords_list)
            
            std_warning = {
                'publish_date': date_obj.strftime('%Y-%m-%d') if date_obj else '',
                'title': warning.get('title', '').strip(),
                'msa': warning.get('msa', ''),
                'matched_keywords': warning.get('matched_keywords', ''),
                'date_range': warning.get('date_range', ''),
                'coordinates': coords_str,
                'summary': warning.get('summary', ''),
                'article_id': warning.get('article_id', ''),
                'url': warning.get('url', ''),
                'scraped_at': warning.get('scraped_at', '')
            }
            
            if std_warning['publish_date'] and std_warning['title']:
                standardized.append(std_warning)
        
        return standardized


def test_scraper():
    """測試爬蟲"""
    print("=" * 80)
    print("MSA Military Exercise Warning Scraper 測試")
    print("=" * 80)
    
    with NavigationWarningScraper(delay=1.5) as scraper:
        # 測試：只爬3頁，過去30天，包含詳細內容
        warnings = scraper.run(max_pages=3, days_back=30, fetch_details=True)
        
        print(f"\n總計: {len(warnings)} 條軍事演習警告\n")
        
        if warnings:
            print("示例數據:")
            for i, w in enumerate(warnings[:2], 1):
                print(f"\n[{i}] {w['title']}")
                print(f"    發布日期: {w['publish_date']}")
                print(f"    日期範圍: {w['date_range']}")
                print(f"    經緯度: {w['coordinates'][:100]}...")
                print(f"    概要: {w['summary'][:100]}...")


if __name__ == '__main__':
    test_scraper()
