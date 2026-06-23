#!/bin/zsh
# 雙擊啟動 local-content-processor 的桌面介面 (lcp gui)。
# 放在專案根目錄。會自動定位自己的位置，所以資料夾搬家也不會壞。

set -u

# 1. 切到這個腳本所在的目錄 = 專案根目錄。
#    lcp gui 預設讀 cwd 下的 config.yaml，所以一定要從專案根目錄執行。
#    ${0:A:h} = 把 $0 解成絕對路徑後取其資料夾 (zsh 內建)。
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || { echo "無法進入目錄 $SCRIPT_DIR"; exit 1; }

LCP="$SCRIPT_DIR/.venv/bin/lcp"

echo "==============================================="
echo "  Local Content Processor — 啟動桌面介面"
echo "  目錄: $SCRIPT_DIR"
echo "==============================================="
echo ""

# 2. 檢查 venv 裡的 lcp 是否存在且可執行。
if [[ ! -x "$LCP" ]]; then
  echo "❌ 找不到可執行的 lcp：$LCP"
  echo ""
  echo "   請先在專案目錄建立虛擬環境並安裝："
  echo "     python3.11 -m venv .venv"
  echo "     ./.venv/bin/pip install -e \".[crawl,media,llm,dedup,gui,dev]\""
  echo ""
  echo "（按 Enter 關閉視窗）"
  read -r _
  exit 1
fi

# 3. 首次執行提醒：還沒有 config.yaml（GUI 的 Settings 面板可以建立，先提示即可）。
if [[ ! -f "config.yaml" ]]; then
  echo "⚠️  目前沒有 config.yaml — 可在 GUI 的 Settings 面板填寫並建立，"
  echo "    或先複製範本：cp config.example.yaml config.yaml 再編輯。"
  echo ""
fi

echo "▶ 啟動中…視窗開啟後，關閉那個視窗就會結束服務。"
echo ""

# 4. 啟動 GUI。webview 視窗會卡住這個終端機直到你關閉視窗。
"$LCP" gui
exit_code=$?

echo ""
if [[ $exit_code -ne 0 ]]; then
  echo "❌ lcp gui 異常結束 (exit $exit_code)，錯誤訊息見上方。"
  echo "（按 Enter 關閉視窗）"
  read -r _
else
  echo "✅ 已關閉服務。"
fi
