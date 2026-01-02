# PLA Aircraft Sorties Data Dashboard

Interactive web dashboard for analyzing PLA aircraft sorties and military activities data (2020-2025).

## 📊 Features

- **Interactive Data Visualization**
  - Time series analysis of PLA aircraft sorties
  - Monthly and yearly distribution charts
  - Weekday pattern analysis
  
- **Data Filtering**
  - Date range selection
  - Multiple dataset support (Comprehensive & Strait Transit)
  
- **Statistics Dashboard**
  - Total records count
  - Average daily sorties
  - Maximum sorties
  - Carrier events tracking
  
- **Data Export**
  - Download filtered data as CSV
  - View raw data in table format

## 🗂️ Project Structure

```
your-repo-name/
├── index.html                          # Main dashboard page
├── data/
│   ├── merged_comprehensive_data_clean.csv  # Comprehensive dataset (2020-2025)
│   └── JapanandBattleship.csv              # Strait transit dataset (2022-2025)
├── README.md                           # This file
└── .gitignore                          # Git ignore file
```

## 🚀 How to Deploy to GitHub Pages

### Step 1: Create GitHub Repository

1. Go to [GitHub](https://github.com) and log in
2. Click the **"+"** button in the top right → **"New repository"**
3. Repository name: `pla-data-dashboard` (or any name you prefer)
4. Description: "Interactive dashboard for PLA aircraft sorties analysis"
5. Make it **Public** (required for GitHub Pages)
6. Click **"Create repository"**

### Step 2: Prepare Your Files

Create the following folder structure on your computer:

```
pla-data-dashboard/
├── index.html
├── data/
│   ├── merged_comprehensive_data_clean.csv
│   └── JapanandBattleship.csv
└── README.md
```

### Step 3: Upload to GitHub

#### Option A: Using GitHub Web Interface (Easiest)

1. In your new repository, click **"Add file"** → **"Upload files"**
2. Drag and drop all files:
   - `index.html`
   - `README.md`
3. Create `data` folder:
   - Click **"Add file"** → **"Create new file"**
   - Type `data/` in the name field
   - Upload both CSV files to this folder
4. Commit changes

#### Option B: Using Git Command Line

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/pla-data-dashboard.git
cd pla-data-dashboard

# Copy your files
# (Copy index.html, README.md, and data folder here)

# Add files
git add .

# Commit
git commit -m "Initial commit: Add PLA data dashboard"

# Push to GitHub
git push origin main
```

### Step 4: Enable GitHub Pages

1. Go to your repository settings (click **"Settings"** tab)
2. Scroll down to **"Pages"** section in the left sidebar
3. Under **"Source"**, select:
   - Branch: `main`
   - Folder: `/ (root)`
4. Click **"Save"**
5. Wait 1-2 minutes for GitHub to build your site
6. Your site will be live at: `https://YOUR_USERNAME.github.io/pla-data-dashboard/`

## 📈 Datasets Description

### 1. Comprehensive Dataset (`merged_comprehensive_data_clean.csv`)
- **Date Range:** 2020-2025
- **Records:** ~2,462 entries
- **Fields:**
  - `date`: Event date
  - `pla_aircraft_sorties`: Number of PLA aircraft sorties
  - `china_carrier_present`: Presence of Chinese aircraft carrier (1/0)
  - `US_Taiwan_interaction`: US-Taiwan military interactions (1/0)
  - `Political_statement`: Political statements made (1/0)
  - `Foreign_battleship`: Foreign battleship presence (1/0)

### 2. Strait Transit Dataset (`JapanandBattleship.csv`)
- **Date Range:** 2022-2025
- **Records:** ~1,356 entries
- **Straits Monitored:**
  - Yonaguni (與那國)
  - Miyako (宮古)
  - Osumi (大禹)
  - Tsushima (對馬)

## 🛠️ Technologies Used

- **HTML5/CSS3**: Structure and styling
- **Bootstrap 4**: Responsive design
- **Chart.js**: Interactive charts
- **PapaParse**: CSV parsing
- **JavaScript**: Data processing and visualization

## 📝 Usage

1. Open the dashboard in your browser
2. Use the date filters to narrow down data
3. Switch between datasets using the dropdown
4. Hover over charts for detailed information
5. Download filtered data using the download button

## 🔧 Customization

To customize the dashboard:

1. **Change Colors**: Edit the CSS variables in the `<style>` section
2. **Modify Charts**: Adjust Chart.js configurations in the JavaScript
3. **Add New Visualizations**: Create new chart containers and initialize them

## 📱 Mobile Responsive

The dashboard is fully responsive and works on:
- Desktop computers
- Tablets
- Mobile phones

## 📄 License

This project is open source and available for educational and research purposes.

## 👤 Author

**Jeremy Chen**
- GitHub: [@s0914712](https://github.com/s0914712)
- Blog: [https://s0914712.github.io](https://s0914712.github.io)

## 📊 Data Sources

PLA aircraft sorties data compiled from public sources including:
- Taiwan Ministry of National Defense
- Japanese Ministry of Defense
- Open-source intelligence reports

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

## ⭐ Show Your Support

Give a ⭐ if this project helped you!

---

**Note:** Ensure your CSV files are properly formatted and placed in the `data/` folder for the dashboard to work correctly.
