import {
  deployModeLabels,
  type EtlApproval,
  type EtlAsset,
  type EtlProdTarget,
  type EtlRelease,
  type EtlReleaseRequest,
  type EtlResult,
} from "../lib/etl";
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
  return (
    <dl className="plan-facts">
      <div>
        <dt>prod 目标</dt>
        <dd>
          <code>
            {target.catalog}.{target.schema}
          </code>
        </dd>
      </div>
      <div>
        <dt>部署方式</dt>
        <dd>{target.mode ? deployModeLabels[target.mode] : "—"}</dd>
      </div>
      <div>
        <dt>运行计划</dt>
        <dd>
          <code>{target.schedule || "未设置"}</code>
          {target.timezone && `（${target.timezone}）`}
        </dd>
      </div>
    </dl>
  );
}

// The release approval: what passed on dev and where it would go.
export function EtlReleaseView({ request }: { request: EtlReleaseRequest }) {
  const dev = request.verification;
  return (
    <section className="panel etl-release" aria-labelledby="etl-release-title">
      <div className="section-heading">
        <h2 id="etl-release-title">
          <Icon name="check" />
          上线审批
        </h2>
        <span className="app-tag">
          <code>{request.bundle}</code>
        </span>
      </div>
      <div className="release-body">
        <h3>dev 试跑</h3>
        {dev ? (
          <div className="release-dev">
            <p>
              {dev.status === "passed"
                ? "通过"
                : dev.status === "failed"
                  ? "未通过"
                  : "未试跑"}
              {dev.mode && ` · ${deployModeLabels[dev.mode]}`}
              {dev.url && (
                <a href={dev.url} target="_blank" rel="noopener noreferrer">
                  在 Databricks 中查看
                </a>
              )}
            </p>
            {dev.tables.length > 0 && (
              <ul>
                {dev.tables.map((table) => (
                  <li key={table.name}>
                    <code>{table.name}</code>：
                    {table.rows === null ? "行数未知" : `${table.rows} 行`}
                    {table.dropped > 0 && `，质量规则丢弃 ${table.dropped} 行`}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="muted">没有 dev 试跑信息。</p>
        )}
        <h3>上线到</h3>
        <ProdTarget target={request.target} />
      </div>
    </section>
  );
}

function ApprovalTimeline({ approvals }: { approvals: EtlApproval[] }) {
  return (
    <ol className="approval-timeline" aria-label="审批记录">
      {approvals.map((approval, i) => (
        <li
          key={i}
          className={approval.decision === "reject" ? "rejected" : ""}
        >
          <div className="timeline-head">
            <strong>{stepLabels[approval.step] ?? approval.step}</strong>
            <span className="timeline-decision">
              {decisionLabels[approval.decision] ?? approval.decision}
            </span>
            <span>{approval.actor || "未署名"}</span>
            <time dateTime={approval.at}>{formatTime(approval.at)}</time>
          </div>
          {approval.comment && <p>意见：{approval.comment}</p>}
        </li>
      ))}
    </ol>
  );
}

function ReleaseResult({ release }: { release: EtlRelease }) {
  if (release.status === "skipped")
    return (
      <p className="muted">
        没有上线到 prod{release.reason ? `：${release.reason}` : "。"}
      </p>
    );
  const link = release.url && (
    <a href={release.url} target="_blank" rel="noopener noreferrer">
      在 Databricks 中查看
    </a>
  );
  if (release.status === "failed")
    return (
      <div className="checks-failed" role="alert">
        <p>
          上线 prod 失败
          {release.catalog && `（${release.catalog}.${release.schema}）`}
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
      已上线到 prod：
      <code>
        {release.catalog}.{release.schema}
      </code>
      {release.mode && ` · ${deployModeLabels[release.mode]}`}
      {release.schedule && ` · 按计划 ${release.schedule} 运行`}
      {link}
    </p>
  );
}

function AssetView({ asset }: { asset: EtlAsset }) {
  if (asset.status === "skipped")
    return (
      <p className="muted">
        没有提交到资产仓库{asset.reason ? `：${asset.reason}` : "。"}
      </p>
    );
  if (asset.status === "failed" || asset.prNumber === null)
    return (
      <div className="checks-failed" role="alert">
        <p>提交到资产仓库{asset.repo && ` ${asset.repo} `}失败</p>
        {asset.errors.length > 0 && (
          <ul>
            {asset.errors.map((error, i) => (
              <li key={i}>{error}</li>
            ))}
          </ul>
        )}
      </div>
    );
  const label = `${asset.repo} PR #${asset.prNumber}${asset.version ? `（v${asset.version}）` : ""}`;
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
        {assetLabels[asset.status] ?? asset.status}
      </span>
    </p>
  );
}

// Decisions so far, the prod deploy and the code's pull request.
export function EtlTrail({ result }: { result: EtlResult }) {
  if (!result.approvals.length && !result.release && !result.asset) return null;
  return (
    <section className="panel etl-trail" aria-labelledby="etl-trail-title">
      <div className="section-heading">
        <h2 id="etl-trail-title">
          <Icon name="runs" />
          审批与上线
        </h2>
      </div>
      <div className="release-body">
        {result.approvals.length > 0 && (
          <>
            <h3>审批记录</h3>
            <ApprovalTimeline approvals={result.approvals} />
          </>
        )}
        {result.release && (
          <>
            <h3>prod 部署</h3>
            <ReleaseResult release={result.release} />
          </>
        )}
        {result.asset && (
          <>
            <h3>代码资产</h3>
            <AssetView asset={result.asset} />
          </>
        )}
      </div>
    </section>
  );
}
