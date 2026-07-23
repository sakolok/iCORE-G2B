const normalizeText = (value) => String(value ?? "").trim().replace(/\s+/g, " ").toLowerCase();

const hasValue = (value) => value !== null && value !== undefined && value !== "";

const makeReason = (key, label, status, detail) => ({ key, label, status, detail });

function organizationMatch(setting, notice, organizationGroups) {
  if (!setting.organizationGroupIds.length) {
    return makeReason("organization", "수요기관", "inactive", "대상 기관 그룹 조건 비활성");
  }

  const selectedGroups = organizationGroups.filter((group) => setting.organizationGroupIds.includes(group.id));
  const agencyName = normalizeText(notice.demandAgencyName);
  const agencyCode = normalizeText(notice.demandAgencyCode);

  const matchedGroup = selectedGroups.find((group) => {
    const names = [
      ...group.parentAgencies,
      ...group.childAgencies,
      ...group.aliases,
    ].map(normalizeText);
    const codes = group.codes.map(normalizeText);
    return names.includes(agencyName) || (agencyCode && codes.includes(agencyCode));
  });

  return matchedGroup
    ? makeReason(
      "organization",
      "수요기관",
      "pass",
      `${notice.demandAgencyName} → ${matchedGroup.name} 그룹 일치`,
    )
    : makeReason(
      "organization",
      "수요기관",
      "fail",
      `${notice.demandAgencyName}이(가) 선택한 기관 목록과 불일치`,
    );
}

function exactMetadataMatch({ key, label, selectedValues, sourceValue }) {
  if (!selectedValues.length) {
    return makeReason(key, label, "inactive", `${label} 조건 비활성`);
  }
  if (!hasValue(sourceValue)) {
    return makeReason(key, label, "pending", `${label} 원본값 확인 필요`);
  }
  return selectedValues.includes(sourceValue)
    ? makeReason(key, label, "pass", `${sourceValue} 일치`)
    : makeReason(key, label, "fail", `${sourceValue} 불일치`);
}

function titleKeywordMatch(setting, notice) {
  const title = normalizeText(notice.bidNtceNm);
  const required = setting.requiredKeywords.filter(Boolean);
  const excluded = setting.excludedKeywords.filter(Boolean);
  const missing = required.filter((keyword) => !title.includes(normalizeText(keyword)));
  const includedExcluded = excluded.filter((keyword) => title.includes(normalizeText(keyword)));

  const requiredReason = !required.length
    ? makeReason("requiredKeywords", "공고명 필수 키워드", "inactive", "필수 키워드 조건 비활성")
    : missing.length
      ? makeReason("requiredKeywords", "공고명 필수 키워드", "fail", `공고명에 누락: ${missing.join(", ")}`)
      : makeReason("requiredKeywords", "공고명 필수 키워드", "pass", `${required.join(", ")} 모두 포함`);

  const excludedReason = !excluded.length
    ? makeReason("excludedKeywords", "공고명 제외 키워드", "inactive", "제외 키워드 조건 비활성")
    : includedExcluded.length
      ? makeReason("excludedKeywords", "공고명 제외 키워드", "fail", `${includedExcluded.join(", ")} 포함`)
      : makeReason("excludedKeywords", "공고명 제외 키워드", "pass", "제외 키워드 미포함");

  return [requiredReason, excludedReason];
}

function baseAmountMatch(setting, notice) {
  const isActive = hasValue(setting.baseAmountMin) || hasValue(setting.baseAmountMax);
  if (!isActive) {
    return makeReason("baseAmount", "기초금액", "inactive", "금액 조건 비활성");
  }
  if (!hasValue(notice.baseAmount)) {
    return makeReason("baseAmount", "기초금액", "pending", "상세 확인 필요 (원본값 없음)");
  }
  const isBelowMin = hasValue(setting.baseAmountMin) && notice.baseAmount < setting.baseAmountMin;
  const isAboveMax = hasValue(setting.baseAmountMax) && notice.baseAmount > setting.baseAmountMax;
  return isBelowMin || isAboveMax
    ? makeReason("baseAmount", "기초금액", "fail", "설정한 금액 범위 불일치")
    : makeReason("baseAmount", "기초금액", "pass", "설정한 금액 범위 일치");
}

function regionMatch(setting, notice) {
  if (!setting.participationRegions.length) {
    return makeReason("region", "참가 가능 지역", "inactive", "지역 조건 비활성");
  }
  if (!Array.isArray(notice.participationRegions)) {
    return makeReason("region", "참가 가능 지역", "pending", "상세 확인 필요 (지역제한 원본값 없음)");
  }
  const isNationwide = notice.participationRegions.includes("전국");
  const overlaps = setting.participationRegions.some((region) => notice.participationRegions.includes(region));
  return isNationwide || overlaps
    ? makeReason("region", "참가 가능 지역", "pass", isNationwide ? "원본값: 전국" : "선택 지역 일치")
    : makeReason("region", "참가 가능 지역", "fail", "선택 지역과 불일치");
}

function proposalDeadlineMatch(setting, notice) {
  const isActive = Boolean(setting.proposalDeadlineStart || setting.proposalDeadlineEnd);
  if (!isActive) {
    return makeReason("proposalDeadline", "제안서 마감일", "inactive", "마감일 조건 비활성");
  }
  if (!notice.proposalDeadline) {
    return makeReason("proposalDeadline", "제안서 마감일", "pending", "상세 확인 필요 (원본값 없음)");
  }
  const proposalDate = notice.proposalDeadline.slice(0, 10);
  const isBeforeStart = setting.proposalDeadlineStart && proposalDate < setting.proposalDeadlineStart;
  const isAfterEnd = setting.proposalDeadlineEnd && proposalDate > setting.proposalDeadlineEnd;
  return isBeforeStart || isAfterEnd
    ? makeReason("proposalDeadline", "제안서 마감일", "fail", "설정한 기간과 불일치")
    : makeReason("proposalDeadline", "제안서 마감일", "pass", "설정한 기간 일치");
}

export function classifyNotice(setting, notice, organizationGroups) {
  const reasons = [
    organizationMatch(setting, notice, organizationGroups),
    exactMetadataMatch({
      key: "workType",
      label: "업무구분",
      selectedValues: setting.workTypes,
      sourceValue: notice.workType,
    }),
    exactMetadataMatch({
      key: "procurementType",
      label: "조달 구분",
      selectedValues: setting.procurementTypes,
      sourceValue: notice.procurementType,
    }),
    ...titleKeywordMatch(setting, notice),
    baseAmountMatch(setting, notice),
    regionMatch(setting, notice),
    proposalDeadlineMatch(setting, notice),
  ];

  const hasClearMismatch = reasons.some((reason) => reason.status === "fail");
  const needsDetailCheck = reasons.some((reason) => reason.status === "pending");

  return {
    ...notice,
    classification: hasClearMismatch ? "EXCLUDE" : needsDetailCheck ? "REVIEW" : "PRIORITY",
    reasons,
  };
}
