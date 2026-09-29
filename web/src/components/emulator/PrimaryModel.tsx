import { GitCompareArrows } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { reverdictText } from '@/components/emulator/reverdict'
import { RunAudit } from '@/components/emulator/RunAudit'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useModels, useSetPrimaryModel } from '@/lib/api/queries'
import type { Run } from '@/lib/api/types'
import { fmtInt, plural } from '@/lib/format'
import { byVersion as byRank, chainOf, versionAuthor, versionLabel } from '@/lib/labels'

/**
 * Event-model versions the ML service scores with (stage 15): the team's v0, the ML developer's v3 and their chain
 * «v3, where it decided, else v0» (lct-95). A loaded version that cannot score yet (`scoring: false`) is not offered:
 * making it primary returns 422. `active` — the primary of new runs.
 */
function useEventVersions(): { versions: string[]; active: string | null } {
  const items = (useModels().data?.items ?? []).filter((m) => m.model === 'smoke_alarm' && m.loaded && m.scoring && m.family !== 'external')
  return { versions: items.map((m) => m.version).sort(byRank), active: items.find((m) => m.active)?.version ?? null }
}

const WHAT_CHANGES =
  'Основная модель выносит вердикты и открывает заявки по новым тревогам. Обе модели оценивают каждую тревогу — оценки и расхождения видны в ленте и журнале. Прошлые вердикты и открытые заявки не меняются.'
/** What a chain choice means, next to the choice itself. */
function ChainNote({ versions }: { versions: string[] }) {
  const chain = versions.find((v) => chainOf(v))
  if (!chain) return null
  const [first, ...rest] = chainOf(chain)!.map(versionLabel)
  return (
    <p className="text-xs text-muted-foreground">
      {versionLabel(chain)} — связка: вердикт по решению {first}, а где {first} тревогу не оценила (день без данных журнала,
      канал не в «Норме» и другие правила) — по {rest.join(', ')}. В ленте и журнале видно, чьё решение.
    </p>
  )
}

const WHAT_CHANGES_PAST =
  'С пересчётом вердикты прошлых тревог прогона тоже станут по новой основной — после подтверждения. Открытые заявки не отзываются, недостающие по её тревогам откроются.'

function Choice({ versions, value, onChange, disabled }: { versions: string[]; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  return (
    <ToggleGroup value={[value]} onValueChange={(v) => v[0] && v[0] !== value && onChange(v[0])} variant="outline" disabled={disabled}>
      {versions.map((v) => (
        <ToggleGroupItem key={v} value={v} className="gap-1.5">
          <span className="font-medium">{versionLabel(v)}</span>
          {versionAuthor(v) && <span className="text-xs text-muted-foreground">{versionAuthor(v)}</span>}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** Confirmation of re-verdicting the past alarms: it changes verdicts and opens work orders, so it is asked once more. */
function ConfirmPast({ run, version, onCancel, onConfirm }: { run: Run; version: string | null; onCancel: () => void; onConfirm: (v: string) => void }) {
  const v = versionLabel(version)
  const same = version === run.primary_model
  return (
    <Dialog open={version !== null} onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{same ? `Пересчитать прошлые вердикты по ${v}?` : `Сделать основной ${v} и пересчитать прошлые вердикты?`}</DialogTitle>
          <DialogDescription>Это меняет данные прогона, отменить пересчёт нельзя — только пересчитать снова по другой версии.</DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1 pl-4 text-sm">
          <li>
            Вердикты {fmtInt(run.alarms)} {plural(run.alarms, ['тревоги', 'тревог', 'тревог'])} прогона — по решениям {v}; где {v} решения
            не выносила, вердикт останется прежним.
          </li>
          <li>По тревогам {v} без открытой заявки откроются заявки. Открытые заявки не отзываются.</li>
          <li>Смена и итог пересчёта попадут в журнал прогона.</li>
        </ul>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Отмена
          </Button>
          <Button onClick={() => version && onConfirm(version)}>{same ? 'Пересчитать' : 'Переключить и пересчитать'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The run's primary event-model version on the console (stage 15): switchable while the run goes; a finished run
 * shows which version it ran with. «Пересчитать и прошлые» re-verdicts the past alarms too (`apply_to_past`), after a
 * confirmation. The switch takes a couple of seconds — a spinner says what is going on; the run's audit is below.
 */
export function PrimaryModelSwitch({ run }: { run: Run }) {
  const { versions: known } = useEventVersions()
  const set = useSetPrimaryModel()
  const [applyToPast, setApplyToPast] = useState(false)
  const [confirm, setConfirm] = useState<string | null>(null)
  const primary = run.primary_model
  if (!primary) return null
  const versions = [...new Set([primary, ...(run.shadow_models ?? []), ...known])].sort(byRank)
  if (versions.length < 2) return null
  const active = run.status === 'running' || run.status === 'paused'
  const change = (version: string, past: boolean) =>
    set.mutate(
      { runId: run.id, version, applyToPast: past },
      {
        onSuccess: (r) => {
          // A re-verdict is a one-off: the next switch goes without it unless asked again.
          setApplyToPast(false)
          toast.success(version === primary ? `Вердикты пересчитаны по ${versionLabel(version)}` : `Основная модель — ${versionLabel(version)}`, {
            description: r.reverdict ? `${reverdictText(r.reverdict)}.` : 'Вердикты и заявки новых тревог — по ней; прошлые не менялись.',
            duration: r.reverdict ? 12_000 : undefined,
          })
        },
        onError: (e) => toast.error(past ? 'Не пересчитано' : 'Не переключилась', { description: e.message }),
      },
    )
  const pending = set.isPending ? set.variables : undefined
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex items-center gap-1.5 text-sm font-medium">
          <GitCompareArrows className="size-4" /> Основная модель
        </span>
        <Choice versions={versions} value={primary} onChange={(v) => (applyToPast ? setConfirm(v) : change(v, false))} disabled={!active || set.isPending} />
        {pending && (
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground" role="status">
            <Spinner />
            {pending.applyToPast ? `пересчитываем вердикты по ${versionLabel(pending.version)}…` : `переключаем на ${versionLabel(pending.version)}…`}
          </span>
        )}
      </div>
      {active && (
        <div className="flex flex-wrap items-center gap-2">
          <Switch id="apply-to-past" checked={applyToPast} onCheckedChange={setApplyToPast} disabled={set.isPending} />
          <Label htmlFor="apply-to-past" className="text-sm font-normal">
            Пересчитать и прошлые тревоги прогона
          </Label>
          {applyToPast && (
            <Button size="sm" variant="outline" onClick={() => setConfirm(primary)} disabled={set.isPending}>
              Пересчитать по {versionLabel(primary)} сейчас
            </Button>
          )}
        </div>
      )}
      <ChainNote versions={versions} />
      <p className="text-xs text-muted-foreground">
        {active ? (applyToPast ? WHAT_CHANGES_PAST : WHAT_CHANGES) : `Прогон шёл с основной ${versionLabel(primary)}; переключить можно только в идущем прогоне.`}
      </p>
      <RunAudit runId={run.id} limit={3} />
      <ConfirmPast
        run={run}
        version={confirm}
        onCancel={() => setConfirm(null)}
        onConfirm={(v) => {
          setConfirm(null)
          change(v, true)
        }}
      />
    </div>
  )
}

/** Choice of the primary version when a run starts; nothing to choose from — nothing shown. */
export function PrimaryModelChoice({ value, onChange }: { value: string | null; onChange: (v: string) => void }) {
  const { versions, active } = useEventVersions()
  if (versions.length < 2) return null
  // Nothing chosen — the run starts with the service's default (the chain when both parts are ready, else v0).
  return (
    <div className="space-y-2">
      <div className="text-sm">Основная модель</div>
      <Choice versions={versions} value={value ?? active ?? versions.find((v) => v === 'baseline-v0') ?? versions[0]} onChange={onChange} />
      <ChainNote versions={versions} />
      <p className="text-xs text-muted-foreground">{WHAT_CHANGES}</p>
    </div>
  )
}
