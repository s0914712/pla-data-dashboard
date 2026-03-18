#!/usr/bin/env python3
"""
===============================================================================
BERT 微調訓練腳本 / BERT Fine-tuning Training Script
===============================================================================

使用 news_classified.json 中已標註的資料微調 bert-base-chinese，
產出兩個分類頭：
  1. 類別分類（9 類）
  2. 情緒分類（3 類：negative / neutral / positive）

用法：
  python -m scripts.classifiers.train_bert_classifier          # 從專案根目錄
  python scripts/classifiers/train_bert_classifier.py          # 直接執行

環境變數：
  BERT_EPOCHS      訓練 epoch 數（預設 10）
  BERT_BATCH_SIZE  batch size（預設 16）
  BERT_LR          學習率（預設 2e-5）
  BERT_MODEL_DIR   模型儲存目錄（預設 models/bert_news_classifier）
"""

import json
import os
import sys
from pathlib import Path
from collections import Counter

import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from transformers import (
    BertTokenizer,
    BertModel,
    get_linear_schedule_with_warmup,
)
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

# ---------------------------------------------------------------------------
# 類別與情緒 label 對照
# ---------------------------------------------------------------------------
CATEGORY_LABELS = [
    "CN_Statement",
    "US_Statement",
    "TW_Statement",
    "Military_Exercise",
    "Foreign_battleship",
    "US_TW_Interaction",
    "Regional_Security",
    "CCP_news_and_blog",
    "Not_Relevant",
]
CATEGORY2ID = {c: i for i, c in enumerate(CATEGORY_LABELS)}

SENTIMENT_LABELS = ["negative", "neutral", "positive"]
SENTIMENT2ID = {s: i for i, s in enumerate(SENTIMENT_LABELS)}

# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
class NewsDataset(Dataset):
    def __init__(self, texts, category_ids, sentiment_ids, tokenizer, max_len=256):
        self.texts = texts
        self.category_ids = category_ids
        self.sentiment_ids = sentiment_ids
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        enc = self.tokenizer(
            self.texts[idx],
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "category_label": torch.tensor(self.category_ids[idx], dtype=torch.long),
            "sentiment_label": torch.tensor(self.sentiment_ids[idx], dtype=torch.long),
        }

# ---------------------------------------------------------------------------
# 多工分類模型
# ---------------------------------------------------------------------------
class BertMultiTaskClassifier(nn.Module):
    """bert-base-chinese + 兩個分類頭"""

    def __init__(self, pretrained_name="bert-base-chinese",
                 num_categories=9, num_sentiments=3, dropout=0.3):
        super().__init__()
        self.bert = BertModel.from_pretrained(pretrained_name)
        hidden = self.bert.config.hidden_size  # 768
        self.dropout = nn.Dropout(dropout)
        self.category_head = nn.Linear(hidden, num_categories)
        self.sentiment_head = nn.Linear(hidden, num_sentiments)

    def forward(self, input_ids, attention_mask):
        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask)
        cls = self.dropout(outputs.pooler_output)  # [B, 768]
        return self.category_head(cls), self.sentiment_head(cls)

# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------
def load_training_data(json_path: str):
    """從 news_classified.json 載入並清洗訓練資料"""
    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    texts, cat_ids, sent_ids = [], [], []
    skipped = 0
    for item in raw:
        cat = item.get("category", "")
        sent = item.get("sentiment_label", "")
        if cat not in CATEGORY2ID or sent not in SENTIMENT2ID:
            skipped += 1
            continue

        # 組合標題+內容作為輸入文本
        article = item.get("original_article", {})
        title = article.get("title", "")
        content = article.get("content", "")[:400]
        text = f"{title} [SEP] {content}" if content else title
        if not text.strip():
            skipped += 1
            continue

        texts.append(text)
        cat_ids.append(CATEGORY2ID[cat])
        sent_ids.append(SENTIMENT2ID[sent])

    print(f"[Train] 載入 {len(texts)} 筆訓練資料（跳過 {skipped} 筆）")
    print(f"[Train] 類別分佈: {Counter(cat_ids)}")
    print(f"[Train] 情緒分佈: {Counter(sent_ids)}")
    return texts, cat_ids, sent_ids

# ---------------------------------------------------------------------------
# 訓練主流程
# ---------------------------------------------------------------------------
def train(
    data_path: str = "data/news_classified.json",
    model_dir: str = "models/bert_news_classifier",
    epochs: int = 10,
    batch_size: int = 16,
    lr: float = 2e-5,
    max_len: int = 256,
    val_ratio: float = 0.15,
    seed: int = 42,
):
    torch.manual_seed(seed)
    np.random.seed(seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[Train] Device: {device}")

    # 1. 載入資料
    texts, cat_ids, sent_ids = load_training_data(data_path)
    if len(texts) < 20:
        print("[Train] ❌ 訓練資料不足 20 筆，無法訓練")
        sys.exit(1)

    # 2. 切分 train / val
    indices = list(range(len(texts)))
    train_idx, val_idx = train_test_split(
        indices, test_size=val_ratio, random_state=seed, stratify=cat_ids
    )
    train_texts = [texts[i] for i in train_idx]
    val_texts = [texts[i] for i in val_idx]
    train_cats = [cat_ids[i] for i in train_idx]
    val_cats = [cat_ids[i] for i in val_idx]
    train_sents = [sent_ids[i] for i in train_idx]
    val_sents = [sent_ids[i] for i in val_idx]

    print(f"[Train] Train: {len(train_texts)}, Val: {len(val_texts)}")

    # 3. Tokenizer
    tokenizer = BertTokenizer.from_pretrained("bert-base-chinese")

    train_ds = NewsDataset(train_texts, train_cats, train_sents, tokenizer, max_len)
    val_ds = NewsDataset(val_texts, val_cats, val_sents, tokenizer, max_len)

    # 加權取樣（處理類別不平衡）
    cat_counts = Counter(train_cats)
    weights = [1.0 / cat_counts[c] for c in train_cats]
    sampler = WeightedRandomSampler(weights, len(weights), replacement=True)

    train_loader = DataLoader(train_ds, batch_size=batch_size, sampler=sampler)
    val_loader = DataLoader(val_ds, batch_size=batch_size)

    # 4. 模型
    model = BertMultiTaskClassifier().to(device)

    # 5. 優化器 + scheduler
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    total_steps = len(train_loader) * epochs
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(total_steps * 0.1),
        num_training_steps=total_steps,
    )

    # 類別權重（處理不平衡）
    cat_weight = torch.tensor(
        [1.0 / max(cat_counts.get(i, 1), 1) for i in range(len(CATEGORY_LABELS))],
        dtype=torch.float,
    ).to(device)
    cat_weight = cat_weight / cat_weight.sum() * len(CATEGORY_LABELS)

    cat_criterion = nn.CrossEntropyLoss(weight=cat_weight)
    sent_criterion = nn.CrossEntropyLoss()

    # 6. 訓練迴圈
    best_val_acc = 0.0
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0
        for batch in train_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            cat_labels = batch["category_label"].to(device)
            sent_labels = batch["sentiment_label"].to(device)

            cat_logits, sent_logits = model(input_ids, attention_mask)
            loss = cat_criterion(cat_logits, cat_labels) + 0.5 * sent_criterion(sent_logits, sent_labels)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(train_loader)

        # --- Validation ---
        model.eval()
        cat_preds, cat_trues = [], []
        sent_preds, sent_trues = [], []
        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)
                cat_logits, sent_logits = model(input_ids, attention_mask)
                cat_preds.extend(cat_logits.argmax(dim=1).cpu().tolist())
                cat_trues.extend(batch["category_label"].tolist())
                sent_preds.extend(sent_logits.argmax(dim=1).cpu().tolist())
                sent_trues.extend(batch["sentiment_label"].tolist())

        cat_acc = sum(p == t for p, t in zip(cat_preds, cat_trues)) / len(cat_trues)
        sent_acc = sum(p == t for p, t in zip(sent_preds, sent_trues)) / len(sent_trues)
        print(
            f"[Epoch {epoch}/{epochs}] loss={avg_loss:.4f}  "
            f"cat_acc={cat_acc:.3f}  sent_acc={sent_acc:.3f}"
        )

        # 儲存最佳模型
        if cat_acc > best_val_acc:
            best_val_acc = cat_acc
            _save_model(model, tokenizer, model_dir, cat_acc, sent_acc)
            print(f"  ✓ Best model saved (cat_acc={cat_acc:.3f})")

    # 7. 最終評估
    print("\n" + "=" * 60)
    print("最終驗證集評估報告：")
    print("=" * 60)
    print("\n--- 類別分類 ---")
    print(classification_report(
        cat_trues, cat_preds,
        target_names=CATEGORY_LABELS,
        zero_division=0,
    ))
    print("--- 情緒分類 ---")
    print(classification_report(
        sent_trues, sent_preds,
        target_names=SENTIMENT_LABELS,
        zero_division=0,
    ))

    print(f"\n[Train] 完成！最佳 cat_acc = {best_val_acc:.3f}")
    print(f"[Train] 模型已儲存至 {model_dir}/")


def _save_model(model, tokenizer, model_dir, cat_acc, sent_acc):
    """儲存模型權重、tokenizer 和 label 對照"""
    out = Path(model_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 儲存完整模型 state_dict（只有分類頭 + BERT）
    torch.save(model.state_dict(), out / "model.pt")

    # 儲存 tokenizer（推論時需要）
    tokenizer.save_pretrained(str(out))

    # 儲存 label 對照
    meta = {
        "category_labels": CATEGORY_LABELS,
        "sentiment_labels": SENTIMENT_LABELS,
        "category2id": CATEGORY2ID,
        "sentiment2id": SENTIMENT2ID,
        "best_cat_acc": cat_acc,
        "best_sent_acc": sent_acc,
        "pretrained_name": "bert-base-chinese",
        "max_len": 256,
    }
    with open(out / "label_config.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    data_path = os.environ.get("BERT_DATA_PATH", "data/news_classified.json")
    model_dir = os.environ.get("BERT_MODEL_DIR", "models/bert_news_classifier")
    epochs = int(os.environ.get("BERT_EPOCHS", "10"))
    batch_size = int(os.environ.get("BERT_BATCH_SIZE", "16"))
    lr = float(os.environ.get("BERT_LR", "2e-5"))

    train(
        data_path=data_path,
        model_dir=model_dir,
        epochs=epochs,
        batch_size=batch_size,
        lr=lr,
    )
