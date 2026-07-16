import time
import hmac
import hashlib
import base64
import json
import requests
import pandas as pd
import gspread
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta


BASE_URL = "https://api.searchad.naver.com"

API_KEY = "01000000000d4de559bd8515cd989bd4ae76535359afb03c701256acf023d9ae1619091fc9"
SECRET_KEY = "AQAAAAANTeVZvYUVzZib1K52U1NZmqVA5PBGsAxIMJMEwM3yMQ=="
CUSTOMER_ID = "1015608"

BASE_DIR = Path(__file__).resolve().parent
GOOGLE_JSON_PATH = (
    BASE_DIR / "peppy-ratio-432702-c7-8ee7addafc50.json"
)
GOOGLE_SHEET_NAME = "myt_naver_report"
PERFORMANCE_SHEET_NAME = "Adgroup_Performance"
BUDGET_SHEET_NAME = "Adgroup_Budget"

KST = ZoneInfo("Asia/Seoul")
now_kst = datetime.now(KST)
today = now_kst.date()

START_DATE = (today - timedelta(days=30)).strftime("%Y-%m-%d")
END_DATE = (today - timedelta(days=1)).strftime("%Y-%m-%d")
TODAY = now_kst.date().strftime("%Y-%m-%d")

print("현재 한국시간 :", now_kst)
print("오늘 :", TODAY)
print("조회 시작 :", START_DATE)
print("조회 종료 :", END_DATE)

def make_signature(timestamp, method, uri, secret_key):
    message = f"{timestamp}.{method}.{uri}"

    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()

    return base64.b64encode(signature).decode("utf-8")


def get_headers(method, uri):
    timestamp = str(int(time.time() * 1000))

    return {
        "X-Timestamp": timestamp,
        "X-API-KEY": API_KEY,
        "X-CUSTOMER": CUSTOMER_ID,
        "X-Signature": make_signature(
            timestamp,
            method,
            uri,
            SECRET_KEY
        ),
        "Content-Type": "application/json"
    }


def request_get(uri, params=None):
    max_retries = 5
    attempt = 0

    while attempt < max_retries:
        headers = get_headers("GET", uri)
        try:
            response = requests.get(
                BASE_URL + uri,
                headers=headers,
                params=params,
                timeout=60
            )

            # Rate Limit (429) 또는 API 일시적 오류(500, 502, 503) 시 대기 후 재시도
            if response.status_code in [429, 500, 502, 503]:
                attempt += 1
                sleep_time = (2 ** attempt) + 1
                print(f"[Warning] API status {response.status_code}. Retrying in {sleep_time} seconds... (Attempt {attempt}/{max_retries})")
                time.sleep(sleep_time)
                continue

            if response.status_code != 200:
                print("API Error:", response.status_code)
                print("Response:", response.text)
                print("Params:", params)
                response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            attempt += 1
            if attempt >= max_retries:
                raise e
            sleep_time = (2 ** attempt) + 1
            print(f"[Warning] Connection error: {e}. Retrying in {sleep_time} seconds... (Attempt {attempt}/{max_retries})")
            time.sleep(sleep_time)


def upload_to_google_sheet(df, spreadsheet_name, worksheet_name):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = Credentials.from_service_account_file(
        GOOGLE_JSON_PATH,
        scopes=scope
    )

    client = gspread.authorize(credentials)
    spreadsheet = client.open(spreadsheet_name)
    worksheet = spreadsheet.worksheet(worksheet_name)

    worksheet.clear()

    values = [
        df.columns.tolist()
    ] + df.fillna("").values.tolist()

    worksheet.update(
        range_name="A1",
        values=values
    )

    print(
        f"Google Sheet upload complete: "
        f"{worksheet_name} / {len(df):,} rows"
    )


def is_deleted(item):
    deleted = item.get("deleted")
    del_flag = item.get("delFlag")
    status = str(item.get("status", "")).upper()

    return (
        deleted is True
        or del_flag is True
        or status in ["DELETED", "REMOVED"]
    )


def is_active(item):
    status = str(item.get("status", "")).upper()
    user_lock = item.get("userLock")

    if is_deleted(item):
        return False

    if user_lock is True:
        return False

    inactive_status = [
        "PAUSED",
        "OFF",
        "SUSPENDED"
    ]

    if status in inactive_status:
        return False

    return True


def get_campaigns():
    """
    성과 보고서에서 과거에 운영했던 캠페인이 누락되지 않도록
    캠페인 상태와 관계없이 전체 캠페인을 가져옵니다.
    """
    uri = "/ncc/campaigns"
    data = request_get(uri)

    rows = []

    for campaign in data:
        rows.append({
            "Campaign ID": campaign.get("nccCampaignId"),
            "Campaign": campaign.get("name"),
            "Campaign Type": campaign.get("campaignTp"),
            "Campaign Status": campaign.get("status"),
            "Campaign User Lock": campaign.get("userLock"),
            "Campaign Deleted": is_deleted(campaign)
        })

    return pd.DataFrame(rows)


def get_all_adgroups(campaigns_df):
    """
    각 캠페인 ID를 순회하며 광고그룹 API를 호출하여 전체 광고그룹을 가져온 뒤,
    성과용과 예산용 데이터로 각각 분리합니다.
    """
    rows = []
    if campaigns_df.empty:
        return pd.DataFrame(rows)

    campaign_ids = campaigns_df["Campaign ID"].dropna().unique().tolist()
    total_campaigns = len(campaign_ids)

    for idx, campaign_id in enumerate(campaign_ids, 1):
        print(f"Retrieving adgroups for campaign: {idx}/{total_campaigns} (ID: {campaign_id})")
        uri = "/ncc/adgroups"
        params = {"nccCampaignId": campaign_id}

        try:
            data = request_get(uri, params=params)

            adgroups = []
            if isinstance(data, list):
                adgroups = data
            elif isinstance(data, dict):
                adgroups = data.get('data') or data.get('items') or []

            for adgroup in adgroups:
                budget = adgroup.get("budget")

                if budget is None:
                    budget = adgroup.get("dailyBudget")

                if budget is None:
                    budget = 0

                rows.append({
                    "Campaign ID": adgroup.get("nccCampaignId"),
                    "Adgroup ID": adgroup.get("nccAdgroupId"),
                    "Adgroup": adgroup.get("name"),
                    "Adgroup Type": str(
                        adgroup.get("adgroupType", "")
                    ).upper(),
                    "Budget": budget,
                    "Adgroup Status": adgroup.get("status"),
                    "Adgroup User Lock": adgroup.get("userLock"),
                    "Adgroup Deleted": is_deleted(adgroup),
                    "Adgroup Active": is_active(adgroup)
                })
        except Exception as error:
            print(f"Failed to get adgroups for campaign {campaign_id}: {error}")

        time.sleep(0.1)

    return pd.DataFrame(rows)


def get_performance_adgroups(all_adgroups_df):
    """
    성과 보고서 대상:
    - WEB_SITE: 파워링크
    - SHOPPING: 쇼핑검색
    - CATALOG: 쇼핑검색 카탈로그형

    조회 기간에 성과가 있었던 중지·삭제 그룹도 찾을 수 있도록
    상태 필터를 적용하지 않습니다.
    """
    performance_types = [
        "WEB_SITE",
        "SHOPPING",
        "CATALOG"
    ]

    result_df = all_adgroups_df[
        all_adgroups_df["Adgroup Type"].isin(
            performance_types
        )
    ].copy()

    return result_df


def get_powerlink_budget_adgroups(all_adgroups_df):
    """
    예산 보고서 대상:
    현재 운영 중인 파워링크 광고그룹만 포함합니다.
    """
    result_df = all_adgroups_df[
        (all_adgroups_df["Adgroup Type"] == "WEB_SITE")
        & (all_adgroups_df["Adgroup Active"] == True)
    ].copy()

    return result_df


def get_adgroup_daily_stats(adgroup_ids, start_date, end_date):
    uri = "/stats"
    rows = []

    total_count = len(adgroup_ids)

    for index, adgroup_id in enumerate(adgroup_ids, start=1):
        print(
            f"Stats request: {index}/{total_count} / "
            f"{adgroup_id}"
        )

        params = {
            "id": adgroup_id,
            "fields": json.dumps([
                "impCnt",
                "clkCnt",
                "salesAmt"
            ]),
            "timeRange": json.dumps({
                "since": start_date,
                "until": end_date
            }),
            "timeIncrement": "1"
        }

        try:
            data = request_get(
                uri,
                params=params
            )

            for row in data.get("data", []):
                rows.append({
                    "Date": row.get("dateStart"),
                    "Adgroup ID": adgroup_id,
                    "Click": row.get("clkCnt", 0),
                    "Impression": row.get("impCnt", 0),
                    "Cost": row.get("salesAmt", 0)
                })

        except requests.RequestException as error:
            print(
                f"Stats request failed: "
                f"{adgroup_id} / {error}"
            )

        time.sleep(0.1)

    return pd.DataFrame(rows)


def make_performance_report(
    stats_df,
    adgroups_df,
    campaigns_df
):
    output_columns = [
        "Date",
        "Campaign",
        "Adgroup",
        "Click",
        "Impression",
        "Cost"
    ]

    if stats_df.empty:
        return pd.DataFrame(columns=output_columns)

    result_df = (
        stats_df
        .merge(
            adgroups_df[
                [
                    "Campaign ID",
                    "Adgroup ID",
                    "Adgroup",
                    "Adgroup Type"
                ]
            ],
            on="Adgroup ID",
            how="left"
        )
        .merge(
            campaigns_df[
                [
                    "Campaign ID",
                    "Campaign"
                ]
            ],
            on="Campaign ID",
            how="left"
        )
    )

    numeric_columns = [
        "Click",
        "Impression",
        "Cost"
    ]

    for column in numeric_columns:
        result_df[column] = pd.to_numeric(
            result_df[column],
            errors="coerce"
        ).fillna(0)

    # 조회 기간 전체 합계가 모두 0인 광고그룹만 제외합니다.
    adgroup_total_df = (
        result_df
        .groupby(
            "Adgroup ID",
            as_index=False,
            dropna=False
        )[
            numeric_columns
        ]
        .sum()
    )

    valid_adgroup_ids = adgroup_total_df.loc[
        (
            adgroup_total_df["Impression"] != 0
        )
        | (
            adgroup_total_df["Click"] != 0
        )
        | (
            adgroup_total_df["Cost"] != 0
        ),
        "Adgroup ID"
    ]

    result_df = result_df[
        result_df["Adgroup ID"].isin(
            valid_adgroup_ids
        )
    ].copy()

    result_df = result_df[
        output_columns
    ]

    return result_df


def make_budget_report(
    budget_adgroups_df,
    campaigns_df,
    today
):
    output_columns = [
        "Date",
        "Campaign",
        "Adgroup",
        "Budget"
    ]

    if budget_adgroups_df.empty:
        return pd.DataFrame(columns=output_columns)

    result_df = (
        budget_adgroups_df
        .merge(
            campaigns_df[
                [
                    "Campaign ID",
                    "Campaign"
                ]
            ],
            on="Campaign ID",
            how="left"
        )
    )

    result_df = result_df[
        [
            "Campaign",
            "Adgroup",
            "Budget"
        ]
    ].copy()

    result_df.insert(
        0,
        "Date",
        today
    )

    result_df["Budget"] = pd.to_numeric(
        result_df["Budget"],
        errors="coerce"
    ).fillna(0)

    return result_df


def sort_performance_report(df):
    if df.empty:
        return df

    df["Date"] = pd.to_datetime(
        df["Date"],
        errors="coerce"
    )

    df = df.sort_values(
        [
            "Date",
            "Campaign",
            "Adgroup"
        ],
        ascending=[
            False,
            False,
            False
        ],
        na_position="last"
    ).reset_index(drop=True)

    df["Date"] = df["Date"].dt.strftime(
        "%Y-%m-%d"
    )

    return df


def sort_budget_report(df):
    if df.empty:
        return df

    df["Date"] = pd.to_datetime(
        df["Date"],
        errors="coerce"
    )

    df = df.sort_values(
        [
            "Date",
            "Campaign",
            "Adgroup"
        ],
        ascending=[
            True,
            True,
            True
        ],
        na_position="last"
    ).reset_index(drop=True)

    df["Date"] = df["Date"].dt.strftime(
        "%Y-%m-%d"
    )

    return df


def main():
    print("Start date:", START_DATE)
    print("End date:", END_DATE)

    campaigns_df = get_campaigns()
    all_adgroups_df = get_all_adgroups(campaigns_df)

    if campaigns_df.empty:
        print("No campaigns found.")
        return

    if all_adgroups_df.empty:
        print("No adgroups found.")
        return

    performance_adgroups_df = get_performance_adgroups(
        all_adgroups_df
    )

    budget_adgroups_df = get_powerlink_budget_adgroups(
        all_adgroups_df
    )

    print(
        "Performance adgroups:",
        len(performance_adgroups_df)
    )

    print(
        "Active Powerlink budget adgroups:",
        len(budget_adgroups_df)
    )

    if performance_adgroups_df.empty:
        performance_df = pd.DataFrame(
            columns=[
                "Date",
                "Campaign",
                "Adgroup",
                "Click",
                "Impression",
                "Cost"
            ]
        )

    else:
        adgroup_ids = (
            performance_adgroups_df["Adgroup ID"]
            .dropna()
            .drop_duplicates()
            .tolist()
        )

        stats_df = get_adgroup_daily_stats(
            adgroup_ids,
            START_DATE,
            END_DATE
        )

        performance_df = make_performance_report(
            stats_df,
            performance_adgroups_df,
            campaigns_df
        )

    budget_df = make_budget_report(
        budget_adgroups_df,
        campaigns_df,
        TODAY
    )

    performance_df = sort_performance_report(
        performance_df
    )

    budget_df = sort_budget_report(
        budget_df
    )

    upload_to_google_sheet(
        performance_df,
        GOOGLE_SHEET_NAME,
        PERFORMANCE_SHEET_NAME
    )

    upload_to_google_sheet(
        budget_df,
        GOOGLE_SHEET_NAME,
        BUDGET_SHEET_NAME
    )

    print("\nPerformance report")
    print(performance_df.head())

    print("\nBudget report")
    print(budget_df.head())

    print("\nCompleted.")
    print(
        "Performance rows:",
        len(performance_df)
    )
    print(
        "Powerlink budget rows:",
        len(budget_df)
    )


if __name__ == "__main__":
    main()
