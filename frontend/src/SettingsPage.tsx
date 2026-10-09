/**
 * 设置页（V0.7.0）：全自动流程模式的开关 + 这条链路会用到的只读运行参数。
 *
 * 这一页只改一件事——上传后是否自动接着识别与复盘。之所以把「切片粒度、模型名、视频保留期」
 * 这些改不了的取值也摆在这里，是因为它们决定「打开开关意味着什么」：开关打开后，每次上传都
 * 会按这个模型、这个切片粒度开始花模型的钱，用户在下决心之前应该看得到这一点。
 *
 * 三条与后端一致性约定：
 *
 * 1. **读对本页所有登录账号开放，写只对管理员**。非管理员看到开关的当前状态与完整说明，
 *    但拿不到可点的入口——看不到状态会让人以为「我的上传行为由别人决定却无从得知」。
 * 2. **写入立即对新的上传生效，不追溯已有任务**。任务在创建时就把开关取值写死，因此本页
 *    不做任何「正在跑的任务会怎样」的承诺，说明文案里也如实写清这一点。
 * 3. **模型未配置时如实提示**。此时打开开关不会自动识别（后端会落成带说明的失败态），
 *    页面上先把这件事说清楚，而不是让人开完开关等一个不会动的任务。
 *
 * 开关的提交不做乐观更新：以服务端返回的取值为准。这个开关影响真实花费，界面显示「开着」
 * 而服务端其实是关的（或反过来）比多等一次请求糟糕得多。
 */

import { useCallback, useEffect, useState } from "react";
import { fetchSettings, updateAutoPipeline, type Settings } from "./api";
import { PLACEHOLDER, formatBytes, formatDuration } from "./format";
import { IconInfo, IconSettings } from "./icons";

/** 保留期按小时展示：秒数对用户没有意义，72 小时才是他需要知道的量级。 */
function formatRetention(seconds: number): string {
  const hours = seconds / 3600;
  if (Number.isInteger(hours)) return `${hours} 小时`;
  return formatDuration(seconds);
}

export default function SettingsPage({ isAdmin }: { isAdmin: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setSettings(await fetchSettings());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "设置读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (next: boolean) => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      // 用返回值覆盖本地状态，而不是直接采纳入参：界面显示的必须是服务端真正落库的取值
      setSettings(await updateAutoPipeline(next));
    } catch (err) {
      setError(err instanceof Error ? err.message : "设置保存失败");
      // 保存失败后重取一次，避免界面停在一个服务端并没有接受的取值上
      await load();
    } finally {
      setSaving(false);
    }
  };

  const enabled = settings?.auto_pipeline ?? false;
  const modelMissing = settings !== null && !settings.llm_configured;

  return (
    <div className="page">
      <h2 className="page-title">设置</h2>

      {loading && <p className="hint">正在读取设置…</p>}

      {error !== null && (
        <p className="detail-error" role="alert">
          {error}
        </p>
      )}

      {!loading && settings !== null && (
        <>
          <div className="panel">
            <div className="panel-head">
              <IconSettings size={18} />
              <h3 className="panel-title">全自动流程模式</h3>
              <span className="panel-head-side">
                <span className="status" data-tone={enabled ? "ok" : undefined}>
                  {enabled ? "已开启" : "已关闭"}
                </span>
              </span>
            </div>

            <div className="setting-row">
              <label className="switch">
                <input
                  type="checkbox"
                  checked={enabled}
                  disabled={!isAdmin || saving}
                  onChange={(event) => void toggle(event.target.checked)}
                  aria-describedby="auto-pipeline-note"
                />
                <span className="switch-track" aria-hidden="true">
                  <span className="switch-thumb" />
                </span>
                <span className="switch-label">
                  {enabled ? "上传后自动完成识别与复盘" : "上传后需手工点击才开始识别"}
                </span>
              </label>
              <p className="hint" id="auto-pipeline-note">
                开启后，任务完成视频处理即自动进入识别，识别结束后自动进入复盘，全程不必再点
                按钮，关闭页面也不影响推进。关闭时恢复为在各阶段手工点击执行。
                本开关只对此后新上传的任务生效，不追溯已有任务。
              </p>
              {!isAdmin && (
                <p className="hint">
                  这个开关只对管理员开放。你的账号可以使用上传、处理与复盘等全部功能；
                  需要改变这项设置请联系管理员。
                </p>
              )}
            </div>

            {enabled && modelMissing && (
              <p className="callout">
                <IconInfo size={16} />
                模型未配置，此时即使开启全自动也不会自动识别：任务会在识别这一步停下并写明原因。
                补齐服务端的 LLM_ 配置后，在任务详情页点「开始识别语音」即可继续。
              </p>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">
              <IconInfo size={18} />
              <h3 className="panel-title">影响这条链路的运行参数</h3>
            </div>
            <dl className="facts-grid" aria-label="运行参数">
              <div>
                <dt className="fact-label">识别与复盘模型</dt>
                <dd className="fact-value">
                  {settings.model_name ?? PLACEHOLDER}
                  {modelMissing && <span className="hint-inline">未配置</span>}
                </dd>
              </div>
              <div>
                <dt className="fact-label">单片时长上限</dt>
                <dd className="fact-value num">
                  {formatDuration(settings.split_max_duration_seconds)}
                </dd>
              </div>
              <div>
                <dt className="fact-label">单片体积上限</dt>
                <dd className="fact-value num">{formatBytes(settings.split_max_clip_bytes)}</dd>
              </div>
              <div>
                <dt className="fact-label">云端视频保留期</dt>
                <dd className="fact-value num">{formatRetention(settings.video_ttl_seconds)}</dd>
              </div>
            </dl>
            <p className="hint mt-md">
              这些取值由服务端的环境变量决定，界面不提供修改入口。列在这里是因为它们决定了
              全自动会按什么粒度跑：片段切得越细，识别调用越多、耗时与费用越高。
            </p>
          </div>
        </>
      )}
    </div>
  );
}
