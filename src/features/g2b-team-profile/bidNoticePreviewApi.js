const LOCAL_G2B_API = "http://127.0.0.1:8000";

export function toPreviewRequest(setting, testResultLimit = null) {
  return {
    collection_setting: {
      name: setting.name,
      memo: setting.memo,
      organization_groups: [],
      work_types: setting.workTypes || [],
      procurement_types: [],
      required_title_keywords: setting.requiredKeywords,
      excluded_title_keywords: setting.excludedKeywords,
      base_amount_min: setting.baseAmountMin,
      base_amount_max: setting.baseAmountMax,
      participation_regions: setting.participationRegions,
      posted_date_start: setting.postedDateStart || null,
      posted_date_end: setting.postedDateEnd || null,
      recipient_emails: setting.recipients,
      instant_priority_alert: setting.instantAlert,
      review_digest_time: setting.digestTime || null,
      google_sheet_target: setting.sheet || null,
    },
    page: 1,
    page_size: 100,
    test_result_limit: testResultLimit,
  };
}

async function request(path, body) {
  const response = await fetch(`${LOCAL_G2B_API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "요청을 처리하지 못했습니다.");
  }
  return payload;
}

export function fetchBidNoticePreview(setting, testResultLimit = null) {
  return request("/api/bid-notice-search/preview", toPreviewRequest(setting, testResultLimit));
}

export function saveSelectedBidNotices(collectionSettingName, selectedItems) {
  return request("/api/bid-notice-search/sheets/selected", {
    collection_setting_name: collectionSettingName,
    selected_items: selectedItems,
  });
}
