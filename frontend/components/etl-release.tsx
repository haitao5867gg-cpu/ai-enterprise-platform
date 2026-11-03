import {
  deployModeLabels,
  type EtlApproval,
  type EtlAsset,
  type EtlProdTarget,
  type EtlRelease,
  type EtlReleaseRequest,
  type EtlResult,
} from "../lib/etl";
import { useT } from "../lib/i18n/client";
import { Icon } from "./icons";

const stepLabels: Record<string, string> = {
  plan: "方案确认",
  release: "上线审批",
};
const decisionLabels: Record<string, string> = {
  approve: "通过",
  reject: "驳回",
};
const assetLabels: Record<string, string> = {
  proposed: "PR 已开，待合并",
  released: "已上线",
  rejected: "上线被驳回",
  release_failed: "上线失败",
};

function formatTime(at: string): string {
  const time = new Date(at);
  return Number.isNaN(time.getTime())
    ? at
    : time.toLocaleString("zh-CN", { hour12: false });
}

function ProdTarget({ target }: { target: EtlProdTarget }) {
  const t = useT();
  return (
    <dl className="plan-facts">
      <div>
        <dt>{t("prod 目标")}</dt>
        <dd>
          <code>
            {target.catalog}.{target.schema}
          </code>
        </dd>
      </div>
      <div>
        <dt>{t("部署方式")}</dt>
        <dd>{target.mode ? t(deployModeLabels[target.mode]) : "—"}</dd>
      </div>
      <div>
        <dt>{t("运行计划")}</dt>
        <dd>
          <code>{target.schedule || t("未设置")}</code>
          {target.timezone && t("（{zone}）", { zone: target.timezone })}
        </dd>
      </div>
    </dl>
  );
}

// The release approval: what passed on dev and where it would go.
export function EtlReleaseView({ request }: { request: EtlReleaseRequest }) {
  const t = useT();
  const dev = request.verification;
  return (
    <section className="panel etl-release" aria-labelledby="etl-release-title">
      <div className="section-heading">
        <h2 id="etl-release-title">
          <Icon name="check" />

          {t("上线审批")}
        </h2>
        <span className="app-tag">
          <code>{request.bundle}</code>
        </span>
      </div>
      <div className="release-body">
        <h3>{t("dev 试跑")}</h3>
        {dev ? (
          <div className="release-dev">
            <p>
              {dev.status === "passed"
                ? t("通过")
                : dev.status === "failed"
                  ? t("未通过")
                  : t("未试跑")}
              {dev.mode && ` · ${t(deployModeLabels[dev.mode])}`}
              {dev.url && (
                <a href={dev.url} target="_blank" rel="noopener noreferrer">
                  {t("在 Databricks 中查看")}
                </a>
              )}
            </p>
            {dev.tables.length > 0 && (
              <ul>
                {dev.tables.map((table) => (
                  <li key={table.name}>
                    <code>{table.name}</code>：
                    {table.rows === null
                      ? t("行数未知")
                      : t("{rows} 行", { rows: table.rows })}
                    {table.dropped > 0 &&
                      t("，质量规则丢弃 {dropped} 行", {
                        dropped: table.dropped,
                      })}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="muted">{t("没有 dev 试跑信息。")}</p>
        )}
        <h3>{t("上线到")}</h3>
        <ProdTarget target={request.target} />
      </div>
    </section>
  );
}

function ApprovalTimeline({ approvals }: { approvals: EtlApproval[] }) {
  const t = useT();
  return (
    <ol className="approval-timeline" aria-label={t("审批记录")}>
      {approvals.map((approval, i) => (
        <li
          key={i}
          className={approval.decision === "reject" ? "rejected" : ""}
        >
          <div className="timeline-head">
            <strong>{t(stepLabels[approval.step] ?? approval.step)}</strong>
            <span className="timeline-decision">
              {t(decisionLabels[approval.decision] ?? approval.decision)}
            </span>
            <span>{approval.actor || t("未署名")}</span>
            <time dateTime={approval.at}>{formatTime(approval.at)}</time>
          </div>
          {approval.comment && (
            <p>{t("意见：{comment}", { comment: approval.comment })}</p>
          )}
        </li>
      ))}
    </ol>
  );
}

function ReleaseResult({ release }: { release: EtlRelease }) {
  const t = useT();
  if (release.status === "skipped")
    return (
      <p className="muted">
        {release.reason
          ? t("没有上线到 prod：{reason}", { reason: release.reason })
          : t("没有上线到 prod。")}
      </p>
    );
  const link = release.url && (
    <a href={release.url} target="_blank" rel="noopener noreferrer">
      {t("在 Databricks 中查看")}
    </a>
  );
  if (release.status === "failed")
    return (
      <div className="checks-failed" role="alert">
        <p>
          {release.catalog
            ? t("上线 prod 失败（{target}）", {
                target: `${release.catalog}.${release.schema}`,
              })
            : t("上线 prod 失败")}
          {link}
        </p>
        {release.errors.length > 0 && (
          <ul>
            {release.errors.map((error, i) => (
              <li key={i}>{error}</li>
            ))}
          </ul>
        )}
      </div>
    );
  return (
    <p className="release-deployed">
      <Icon name="check" size={16} />

      {t("已上线到 prod：")}
      <code>
        {release.catalog}.{release.schema}
      </code>
      {release.mode && ` · ${t(deployModeLabels[release.mode])}`}
      {release.schedule &&
        t(" · 按计划 {schedule} 运行", { schedule: release.schedule })}
      {link}
    </p>
  );
}

function AssetView({ asset }: { asset: EtlAsset }) {
  const t = useT();
  if (asset.status === "skipped")
    return (
      <p className="muted">
        {asset.reason
          ? t("没有提交到资产仓库：{reason}", { reason: asset.reason })
          : t("没有提交到资产仓库。")}
      </p>
    );
  if (asset.status === "failed" || asset.prNumber === null)
    return (
      <div className="checks-failed" role="alert">
        <p>
          {asset.repo
            ? t("提交到资产仓库 {repo} 失败", { repo: asset.repo })
            : t("提交到资产仓库失败")}
        </p>
        {asset.errors.length > 0 && (
          <ul>
            {asset.errors.map((error, i) => (
              <li key={i}>{error}</li>
            ))}
          </ul>
        )}
      </div>
    );
  const label = asset.version
    ? t("{repo} PR #{n}（v{version}）", {
        repo: asset.repo ?? "",
        n: asset.prNumber,
        version: asset.version,
      })
    : `${asset.repo} PR #${asset.prNumber}`;
  return (
    <p className="asset-pr">
      {asset.prUrl ? (
        <a href={asset.prUrl} target="_blank" rel="noopener noreferrer">
          {label}
          <Icon name="arrow" size={14} />
        </a>
      ) : (
        label
      )}
      <span className="app-tag">
        {t(assetLabels[asset.status] ?? asset.status)}
      </span>
    </p>
  );
}

// Decisions so far, the prod deploy and the code's pull request.
export function EtlTrail({ result }: { result: EtlResult }) {
  const t = useT();
  if (!result.approvals.length && !result.release && !result.asset) return null;
  return (
    <section className="panel etl-trail" aria-labelledby="etl-trail-title">
      <div className="section-heading">
        <h2 id="etl-trail-title">
          <Icon name="runs" />

          {t("审批与上线")}
        </h2>
      </div>
      <div className="release-body">
        {result.approvals.length > 0 && (
          <>
            <h3>{t("审批记录")}</h3>
            <ApprovalTimeline approvals={result.approvals} />
          </>
        )}
        {result.release && (
          <>
            <h3>{t("prod 部署")}</h3>
            <ReleaseResult release={result.release} />
          </>
        )}
        {result.asset && (
          <>
            <h3>{t("代码资产")}</h3>
            <AssetView asset={result.asset} />
          </>
        )}
      </div>
    </section>
  );
}
