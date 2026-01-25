---
title: "使用機器學習 預測軍事動態-航母篇"
date: 2024-11-29T10:07:47+06:00
draft: false

# post thumb
image: "images/post/post-2.jpg"

# meta description
description: "使用機器學習 預測航母軍事動態"

# taxonomies
categories: 
  - "AI解決軍事問題"
tags:
  - "AI"
  - "Military"
  - "PLA"
  - "軍事動態"
  - "航母"
  - "深度學習"

# post type
type: "post"
---

<hr>


我們蒐集到日本防衛省統合幕僚監部 下載 2023-2024年經過日本的航母動態 整理成csv檔
資料集長得像下面這樣

|  DATE      |  Intel  |  BZK  |  battleshi1(<3)  |  battleshi1(>3)  |  carrier  |  WZ7  |  R_Navy  |  H6  |  Y-9  |  Russia Air  |  Warning  |  Taiwan Air Activity  |  Taiwan 1LA Exerise  |  month  |  in 5 Eay inetel  |  in 5 Eay Russiashi1  |  in 5 Eay battleshi1  |  in5EayH6Y9  |  is5datCARRIER  |
| ------------ | --------- | ------- | ------------------ | ------------------ | ----------- | ------- | ---------- | ------ | ------- | -------------- | ----------- | ----------------------- | ---------------------- | --------- | ------------------- | ----------------------- | ----------------------- | -------------- | ----------------- |
|  20230101  |         |       |                  |  K               |  K        |  K    |          |      |       |              |  1        |  19                   |  0                   |  1      |  0                |  0                    |  1                    |  0           |  FALSE          |
|  20230102  |         |       |                  |  K               |  K        |  K    |          |      |       |              |  1        |  0                    |  0                   |  1      |  0                |  0                    |  2                    |  0           |  TRUE           |
|  20230103  |         |       |                  |                  |           |       |          |      |       |              |  1        |  0                    |  0                   |  1      |  0                |  0                    |  4                    |  2           |  TRUE           |
|  20230104  |  T      |       |  T               |                  |           |       |          |      |       |              |  1        |  3                    |  0                   |  1      |  0                |  0                    |  1                    |  0           |  FALSE          |
|  20230105  |         |       |                  |                  |           |       |          |      |       |              |  1        |  3                    |  0                   |  1      |  1                |  0                    |  3                    |  0           |  FALSE          |
|  20230106  |         |       |                  |                  |           |       |          |      |       |              |  1        |  3                    |  0                   |  1      |  1                |  0                    |  3                    |  0           |  FALSE          |

試著把各種不同的機種出現都列出來  看機器學習  能不能辦到預測航母的出現 應該用LSTM 來做會比較準確  但實驗性質  我們就把5年內出現與否當作一個參數 使用RrandomForest 或XGBoost 分類法就好
 [點我下載](https://drive.google.com/file/d/1jIyhJ47ykU1ZKyUhRdORc_zEjOZxo7_H/view?usp=drive_link "Google's Homepage")
資料集說明 
1. carrier 是我們想要預測的對象
2. Intel  情報船
3. BZK    無人機
4. battleship 戰艦
5. R_Navy 俄羅斯海軍
6. Russia Air俄羅斯空軍
7, Warning 航行警告 發布
8. Taiwan Air Activity 臺灣地區軍事動態
9. in 5 Day ... 在五天內是否有出現....
## 開始讀取資料  寫程式
```
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report
data_path='Japan.csv'
def load_and_preprocess_data(data_path):
    """
    Load and preprocess the data
    """
    df = pd.read_csv(data_path)
    
    # Check for unexpected values in carrier column
    expected_values = {'K', 'N', 'E', np.nan}
    unexpected_values = set(df['carrier'].unique()) - expected_values
    
    if unexpected_values:
        print("\nWarning: Unexpected values found in carrier column:")
        print(f"Unexpected values: {unexpected_values}")
        print("\nRows with unexpected values:")
        for value in unexpected_values:
            unexpected_rows = df[df['carrier'] == value]
            print(f"\nValue '{value}' appears in rows:")
            print(unexpected_rows[['DATE', 'carrier']].to_string())
    
    df['carrier'] = df['carrier'].fillna('N')
    return df

def prepare_features(df):
    """
    Prepare features for the model
    """
    features = [
        'Intel', 'BZK', 
        'battleship(<3)', 'battleship(>3)',
        'WZ7', 'R_Navy', 'H6', 'Y-9',
        'Russia Air', 'Warning',
        'Taiwan Air Activity', 'Taiwan PLA Exerise',
        'month', 
        'in 5 day intel', 'in 5 day Russiaship',
        'in 5 day battleship', 'in5dayH6Y'
    ]
    
    # Verify available features
    available_features = [f for f in features if f in df.columns]
    
    # Data preprocessing
    for feature in available_features:
        df[feature] = pd.to_numeric(df[feature], errors='coerce').fillna(0)
        
    return df[available_features], available_features

def train_model(X, y):
    """
    Train the Random Forest model with handling for small datasets
    """
    le = LabelEncoder()
    y_encoded = le.fit_transform(y)
    
    # Check class distribution
    unique_classes, class_counts = np.unique(y_encoded, return_counts=True)
    min_samples = min(class_counts)
    
    if min_samples < 2:
        print(f"Warning: Very small dataset detected. Using all data for training.")
        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=5,
            min_samples_split=2,
            min_samples_leaf=1,
            random_state=42
        )
        model.fit(X, y_encoded)
        return model, le, (X, y_encoded)
    else:
        # Normal split and training if enough samples
        X_train, X_test, y_train, y_test = train_test_split(
            X, y_encoded, 
            test_size=0.2, 
            random_state=42,
            stratify=y_encoded
        )
        
        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=10,
            min_samples_split=5,
            min_samples_leaf=2,
            random_state=42
        )
        
        model.fit(X_train, y_train)
        return model, le, (X_test, y_test)

def evaluate_model(model, X_test, y_test):
    """
    Evaluate model performance with proper handling of all classes
    """
    y_pred = model.predict(X_test)
    print("\nModel Evaluation:")
    print(f"Accuracy: {accuracy_score(y_test, y_pred):.4f}")
    
    # Get actual unique classes from the data
    unique_classes = sorted(np.unique(y_test))
    # Get class names from label encoder
    class_names = ['K', 'N', 'E']
    
    # Ensure we have all class names for the report
    actual_class_names = [class_names[i] for i in unique_classes]
    
    print("\nClassification Report:")
    try:
        print(classification_report(y_test, y_pred, target_names=actual_class_names))
    except Exception as e:
        print("Detailed class-wise metrics:")
        # Manual calculation of metrics for each class
        for class_idx, class_name in zip(unique_classes, actual_class_names):
            class_mask = (y_test == class_idx)
            class_pred_mask = (y_pred == class_idx)
            class_correct = np.sum((y_test == y_pred) & class_mask)
            class_total = np.sum(class_mask)
            class_precision = np.sum((y_test == y_pred) & class_pred_mask) / (np.sum(class_pred_mask) + 1e-10)
            class_recall = class_correct / (class_total + 1e-10)
            class_f1 = 2 * (class_precision * class_recall) / (class_precision + class_recall + 1e-10)
            
            print(f"\nClass: {class_name}")
            print(f"Samples: {class_total}")
            print(f"Precision: {class_precision:.4f}")
            print(f"Recall: {class_recall:.4f}")
            print(f"F1-score: {class_f1:.4f}")

def predict_carrier(model, le, features, new_data):
    """
    Make prediction for new data
    """
    df_new = pd.DataFrame([new_data])
    for feature in features:
        if feature not in df_new.columns:
            df_new[feature] = 0
    
    df_new = df_new[features]
    pred = model.predict(df_new)
    prob = model.predict_proba(df_new)
    
    return le.inverse_transform(pred)[0], prob[0]

if __name__ == "__main__":
    data_path = "Japan.csv"
    
    try:
        print("Loading data from:", data_path)
        # Load and preprocess data
        df = load_and_preprocess_data(data_path)
        
        # Prepare features
        X, features = prepare_features(df)
        y = df['carrier']
        
        # Print basic statistics
        total = len(df)
        value_counts = y.value_counts()
        print("\nDetailed carrier value counts:")
        print(value_counts)
        print("\nBasic Statistics:")
        print(f"Total Records: {total}")
        
        # Print counts for each value, including unexpected ones
        for value in value_counts.index:
            count = value_counts[value]
            percentage = count/total
            print(f"Carrier appearances as '{value}': {count} ({percentage:.2%})")
        
        # Train model
        model, label_encoder, (X_test, y_test) = train_model(X, y)
        
        # Evaluate model
        evaluate_model(model, X_test, y_test)
        
        # Feature importance
        importance = pd.DataFrame({
            'feature': features,
            'importance': model.feature_importances_
        }).sort_values('importance', ascending=False)
        print("\nFeature Importance:")
        print(importance)
        
        # Example prediction(這邊給一個樣本讓他預測)
        new_data = {f: 0 for f in features}
        new_data.update({
            'R_Navy': 1,
            'month': 1,
            'in 5 day Russiaship': 1
        })
        
        prediction, probabilities = predict_carrier(model, label_encoder, features, new_data)
        print("\nPrediction Results:")
        print(f"Predicted Location: {prediction}")
        
        # Print probabilities for all classes
        class_names = label_encoder.classes_  # Use actual classes from encoder
        for class_name, prob in zip(class_names, probabilities):
            print(f"Probability of {class_name}: {prob:.2%}")
        
    except FileNotFoundError:
        print(f"Error: File '{data_path}' not found")
    except Exception as e:
        print(f"Error: {str(e)}")
```
<hr>

## 以下是程式的結果

```
Loading data from: Japan.csv

Detailed carrier value counts:
carrier
N    617
K    113
Name: count, dtype: int64

Basic Statistics:
Total Records: 730
Carrier appearances as 'N': 617 (84.52%) 沒出現航母的樣本數
Carrier appearances as 'K': 113 (15.48%) 出現航母的樣本數
模型的評估
Model Evaluation:
Accuracy: 0.9863

                                    Classification Report:
                             precision    recall  f1-score   support

預測航母出現的準確率   K       0.96      0.96      0.96        23
預測航母沒出現的準確率 N       0.99      0.99      0.99       123

               accuracy                           0.99       146
              macro avg       0.97      0.97      0.97       146
           weighted avg       0.99      0.99      0.99       146

#recall 為預測為正的數量(有可能沒出現也給他預測出現)

Feature Importance:
                feature  importance
10        is5datCARRIER    0.893500
9                 month    0.059949
8   Taiwan Air Activity    0.036262
7               Warning    0.010289
0                 Intel    0.000000
1                   BZK    0.000000
2                   WZ7    0.000000
3                R_Navy    0.000000
4                    H6    0.000000
5                   Y-9    0.000000
6            Russia Air    0.000000
給予的樣本預測
Prediction Results:
Predicted Location: N
Probability of K: 0.05%
Probability of N: 99.95%

```

## 結論

我們可以看到分類的狀況還蠻好的，但是航母出現的資料數量其實蠻少的，就算全部猜不出現，也能猜對85%，我們看分類出來 第一個是看前五天有沒有出現，再來按照月份、再來是臺灣地區空中動態與航行警告的發布，因此可以知道這幾個因素有相關聯
2023至2024年11月份，航母活動在西太平洋區域而被日方偵測到的天數有113天，未出現的天數為617天，運用機器學習的分類技巧可以預測航母未出現的機率，我們可以觀察到AI在判斷是否有航母出現時首先先檢查前一天是否有航母出現，其次是月份(共軍航母在2023年至2024年通常於9-10月出現比例較大)還有臺灣地區的共機動態(如圖)、航行警告的發布等等都可以作為預判航母出航的徵兆，因此在機器學習技術成熟的今日，在探索軍事動態相關因素上吾人更應該廣泛地蒐集可能原因，然後藉由分析技術預測。
#### 不過這個模型也只能掌握了某些邏輯，無法100%預測
