# 📦 GitHub 部署完整指南（中文版）

## 🎯 目標
將您的 PLA 軍事數據儀表板部署到 GitHub Pages，讓全世界都能訪問！

---

## 📋 步驟一：準備文件

### 1. 創建項目文件夾

在您的電腦上創建一個新文件夾，例如：
```
C:\Users\YourName\pla-data-dashboard\
```

### 2. 放入以下文件

```
pla-data-dashboard/
├── index.html                          ← 主頁面
├── README.md                           ← 項目說明文件
├── .gitignore                          ← Git忽略文件
└── data/                               ← 數據文件夾
    ├── merged_comprehensive_data_clean.csv
    └── JapanandBattleship.csv
```

**重要：** 所有文件我都已經為您準備好了！

---

## 🚀 步驟二：上傳到 GitHub（網頁版 - 最簡單！）

### 方法 A：使用 GitHub 網頁介面（推薦給初學者）

#### 1. 創建 GitHub 帳號（如果還沒有）
- 前往 https://github.com
- 點擊右上角 "Sign up" 註冊
- 驗證您的電子郵件

#### 2. 創建新的 Repository（倉庫）
1. 登入 GitHub 後，點擊右上角 **"+"** → **"New repository"**
2. 填寫資訊：
   - **Repository name**: `pla-data-dashboard` （或您喜歡的名字）
   - **Description**: "共軍活動數據互動式儀表板"
   - **Public** ✅ （必須選公開才能使用 GitHub Pages）
   - **不要勾選** "Add a README file"（我們已經有了）
3. 點擊綠色按鈕 **"Create repository"**

#### 3. 上傳文件

##### 3.1 上傳主要文件
1. 在新建的 repository 頁面，點擊 **"uploading an existing file"** 連結
2. 拖曳以下文件到瀏覽器視窗：
   - `index.html`
   - `README.md`
   - `.gitignore`
3. 在底部的 "Commit changes" 區域，輸入：
   ```
   Add main files
   ```
4. 點擊綠色按鈕 **"Commit changes"**

##### 3.2 上傳數據文件
1. 回到 repository 主頁
2. 點擊 **"Add file"** → **"Upload files"**
3. 點擊 **"Create new file"**
4. 在文件名輸入框輸入：`data/.gitkeep`（這會創建 data 文件夾）
5. 點擊 **"Commit new file"**
6. 再次點擊 **"Add file"** → **"Upload files"**
7. 選擇 `data/` 作為上傳位置
8. 拖曳以下文件：
   - `merged_comprehensive_data_clean.csv`
   - `JapanandBattleship.csv`
9. 點擊 **"Commit changes"**

---

## 方法 B：使用 Git 命令行（進階用戶）

### 1. 安裝 Git
- Windows: 下載 https://git-scm.com/download/win
- Mac: 打開 Terminal 輸入 `git --version`（會自動安裝）

### 2. 配置 Git（首次使用）
```bash
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

### 3. 上傳文件
```bash
# 1. 進入您的項目文件夾
cd C:\Users\YourName\pla-data-dashboard

# 2. 初始化 Git
git init

# 3. 添加遠程倉庫（替換 YOUR_USERNAME 為您的 GitHub 用戶名）
git remote add origin https://github.com/YOUR_USERNAME/pla-data-dashboard.git

# 4. 添加所有文件
git add .

# 5. 提交
git commit -m "Initial commit: 新增共軍數據儀表板"

# 6. 推送到 GitHub
git branch -M main
git push -u origin main
```

---

## 🌐 步驟三：啟用 GitHub Pages

### 1. 進入 Settings
在您的 repository 頁面，點擊頂部的 **"Settings"** 標籤

### 2. 配置 Pages
1. 在左側邊欄找到並點擊 **"Pages"**
2. 在 **"Build and deployment"** 區域：
   - **Source**: 選擇 **"Deploy from a branch"**
   - **Branch**: 選擇 **"main"** 和 **"/ (root)"**
3. 點擊 **"Save"**

### 3. 等待部署
- 等待 1-2 分鐘
- 重新整理頁面
- 您會看到一個綠色的成功訊息：
  ```
  Your site is live at https://YOUR_USERNAME.github.io/pla-data-dashboard/
  ```

---

## ✅ 步驟四：測試您的網站

### 1. 訪問網站
打開瀏覽器，前往：
```
https://YOUR_USERNAME.github.io/pla-data-dashboard/
```

### 2. 檢查功能
- ✅ 頁面正常載入
- ✅ 統計卡片顯示數字
- ✅ 圖表正常顯示
- ✅ 可以篩選日期
- ✅ 可以下載數據

---

## 🔧 常見問題解決

### ❌ 問題 1：頁面顯示 404 Not Found
**解決方案：**
- 檢查 repository 是否設為 **Public**
- 確認 GitHub Pages 已啟用
- 等待 5-10 分鐘讓 GitHub 完成部署

### ❌ 問題 2：圖表不顯示
**解決方案：**
- 檢查 `data/` 文件夾中的 CSV 文件是否正確上傳
- 打開瀏覽器的開發者工具（F12）查看錯誤訊息
- 確認 CSV 文件路徑為：
  - `data/merged_comprehensive_data_clean.csv`
  - `data/JapanandBattleship.csv`

### ❌ 問題 3：無法上傳大文件
**解決方案：**
- GitHub 單個文件限制 100MB
- 如果 CSV 文件太大，可以壓縮或分割
- 使用 Git LFS（Large File Storage）

---

## 📱 分享您的網站

### 複製這個網址分享給朋友：
```
https://YOUR_USERNAME.github.io/pla-data-dashboard/
```

### 嵌入到您的部落格：
```html
<iframe src="https://YOUR_USERNAME.github.io/pla-data-dashboard/" 
        width="100%" height="800px" frameborder="0">
</iframe>
```

---

## 🎨 自定義您的儀表板

### 修改標題
編輯 `index.html` 第 7 行：
```html
<title>您的標題 - Chen Blog</title>
```

### 修改顏色主題
編輯 `index.html` 的 CSS 部分：
```css
.dashboard-header {
    background: linear-gradient(135deg, #您的顏色1 0%, #您的顏色2 100%);
}
```

### 添加您的 Logo
在 `index.html` 的 header 區域添加：
```html
<img src="your-logo.png" alt="Logo" style="height: 50px;">
```

---

## 📊 更新數據

### 當您有新的 CSV 數據時：

1. 前往您的 GitHub repository
2. 點擊 `data/` 文件夾
3. 點擊要更新的 CSV 文件
4. 點擊右上角的鉛筆圖標 ✏️（編輯）
5. 刪除舊內容，貼上新內容
6. 點擊 **"Commit changes"**
7. 等待 1-2 分鐘，網站會自動更新

---

## 🎓 進階功能

### 添加 Google Analytics
在 `index.html` 的 `</head>` 前添加：
```html
<!-- Google Analytics -->
<script async src="https://www.googletagmanager.com/gtag/js?id=您的GA-ID"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', '您的GA-ID');
</script>
```

### 添加自定義域名
1. 購買域名（如 GoDaddy、Namecheap）
2. 在 GitHub Pages 設置中添加 Custom domain
3. 在域名提供商設置 DNS 記錄

---

## 📞 需要幫助？

### 資源連結：
- [GitHub Pages 官方文檔](https://docs.github.com/en/pages)
- [Git 教學](https://git-scm.com/book/zh-tw/v2)
- [HTML/CSS 教學](https://www.w3schools.com)

### 常用指令速查：
```bash
git status          # 查看狀態
git add .           # 添加所有更改
git commit -m "..."  # 提交更改
git push            # 推送到 GitHub
git pull            # 從 GitHub 拉取最新版本
```

---

## 🎉 完成！

恭喜！您的數據儀表板現在已經在線上了！

**您的網站：** `https://YOUR_USERNAME.github.io/pla-data-dashboard/`

現在您可以：
- ✅ 分享給同事和朋友
- ✅ 嵌入到您的部落格
- ✅ 用於研究和分析
- ✅ 隨時更新數據

---

**製作者：** Jeremy Chen  
**最後更新：** 2025年1月

有任何問題歡迎到 GitHub Issues 提出！ 🚀
