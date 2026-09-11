import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { AiAnsweringMode, AiAnsweringSettings, AutoCheckinMode, AutoCheckinSettings, CheckinSourceSettings, NotificationSub as VoiceConfig, CourseItem } from '../types'
import { useAccounts } from '../hooks/useAccounts'

interface ActiveLesson {
  lessonid: number
  lessonname: string
  classroomid: number
  teacher_name: string | null
}

interface ProblemReview {
  problem_id: string | number
  problem_type: number
  content: string
  cover_url?: string
  options: { key: string; text: string }[]
}

interface PendingAnswer {
  answer_id: string
  lesson?: string
  lessonid?: string | number
  problemid?: string | number
  problemtype?: number
  problem: ProblemReview
  answer: unknown
  source: 'ai' | 'random' | 'off'
  answer_ready: boolean
}

interface ActivityEvent {
  id: number
  timestamp: string
  type: string
  lesson?: string
  lessonid?: number
  status?: string
  message?: string
  content?: string
  answers?: unknown
  problemid?: unknown
  problemtype?: number
  source?: string | number
  pending_answer_id?: string
}

interface QRCheckinState {
  status: 'scanning' | 'success' | 'error'
  lesson_id?: string | number
  already_running?: boolean
  code?: string | number
  message?: string
}

const VOICE_SUBOPTION: Partial<Record<string, keyof Omit<VoiceConfig, 'enabled'>>> = {
  signin: 'signin',
  problem: 'problem',
  problem_received: 'problem',
  call: 'call',
  danmu: 'danmu',
  red_packet: 'red_packet',
}

let eventCounter = 0

function formatAnswer(answer: unknown): string {
  if (Array.isArray(answer)) return answer.join(', ')
  if (answer && typeof answer === 'object') return JSON.stringify(answer)
  return answer == null ? '' : String(answer)
}

function normalizeAiAnsweringMode(settings: AiAnsweringSettings): AiAnsweringMode {
  if (settings.ai_answering_mode === 'ai' || settings.ai_answering_mode === 'random' || settings.ai_answering_mode === 'off') {
    return settings.ai_answering_mode
  }
  return settings.ai_answering_enabled ? 'ai' : 'off'
}

function normalizeAutoCheckinMode(settings: AutoCheckinSettings): AutoCheckinMode {
  if (settings.auto_checkin_mode === 'on' || settings.auto_checkin_mode === 'scheduled' || settings.auto_checkin_mode === 'off') {
    return settings.auto_checkin_mode
  }
  return settings.auto_checkin ? 'on' : 'off'
}

function normalizePendingAnswer(message: Record<string, unknown>): PendingAnswer | null {
  if (typeof message.answer_id !== 'string' || !message.problem || typeof message.problem !== 'object') {
    return null
  }

  const raw = message.problem as Record<string, unknown>
  const options = Array.isArray(raw.options)
    ? raw.options.flatMap((option) => {
      if (!option || typeof option !== 'object') return []
      const item = option as Record<string, unknown>
      return [{
        key: String(item.key ?? ''),
        text: typeof item.text === 'string' ? item.text : '',
      }]
    })
    : []

  const problemType = typeof raw.problem_type === 'number'
    ? raw.problem_type
    : typeof message.problemtype === 'number' ? message.problemtype : 0

  return {
    answer_id: message.answer_id,
    lesson: typeof message.lesson === 'string' ? message.lesson : undefined,
    lessonid: typeof message.lessonid === 'string' || typeof message.lessonid === 'number' ? message.lessonid : undefined,
    problemid: typeof message.problemid === 'string' || typeof message.problemid === 'number' ? message.problemid : undefined,
    problemtype: typeof message.problemtype === 'number' ? message.problemtype : undefined,
    problem: {
      problem_id: typeof raw.problem_id === 'string' || typeof raw.problem_id === 'number' ? raw.problem_id : '',
      problem_type: problemType,
      content: typeof raw.content === 'string' ? raw.content : '',
      cover_url: typeof raw.cover_url === 'string' ? raw.cover_url : '',
      options,
    },
    answer: message.answer,
    source: message.source === 'random' ? 'random' : message.source === 'off' ? 'off' : 'ai',
    answer_ready: message.answer_ready === true || (message.answer_ready === undefined && message.answer != null),
  }
}

function AnswerReviewModal({
  answer,
  busy,
  error,
  onConfirm,
  onSkip,
}: {
  answer: PendingAnswer
  busy: boolean
  error: string
  onConfirm: () => void
  onSkip: () => void
}) {
  const { t } = useTranslation()
  const problem = answer.problem
  const problemTypeLabel = problem.problem_type ? t(`events.problemType${problem.problem_type}`) : ''

  return (
    <div className="answer-review-backdrop">
      <section
        className="answer-review-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="answer-review-title"
      >
        <div className="answer-review-header">
          <div>
            <span className="answer-review-eyebrow">{t('dashboard.answerReviewEyebrow')}</span>
            <h2 id="answer-review-title">{t('dashboard.answerReviewTitle')}</h2>
            <p>{answer.lesson ? `${answer.lesson}${problemTypeLabel ? ` · ${problemTypeLabel}` : ''}` : problemTypeLabel}</p>
          </div>
        </div>

        {problem.cover_url && (
          <img className="answer-review-image" src={problem.cover_url} alt={t('dashboard.answerReviewImageAlt')} />
        )}

        <div className="answer-review-question">
          <span className="answer-review-label">{t('dashboard.answerReviewQuestion')}</span>
          <p>{problem.content || t('dashboard.answerReviewNoText')}</p>
        </div>

        {problem.options.length > 0 && (
          <ol className="answer-review-options">
            {problem.options.map((option) => (
              <li key={option.key}>
                <strong>{option.key}</strong>
                <span>{option.text || option.key}</span>
              </li>
            ))}
          </ol>
        )}

        <div className="answer-review-answer">
          <span className="answer-review-label">
            {answer.source === 'off'
              ? t('dashboard.answerReviewOff')
              : answer.answer_ready
                ? answer.source === 'ai' ? t('dashboard.answerReviewAiAnswer') : t('dashboard.answerReviewRandomAnswer')
                : t('dashboard.answerReviewWaiting')}
          </span>
          {answer.answer_ready && <strong>{formatAnswer(answer.answer)}</strong>}
        </div>

        {error && <p className="answer-review-error" role="alert">{error}</p>}
        <div className="answer-review-actions">
          <button className="btn btn-secondary" onClick={onSkip} disabled={busy}>
            {busy
              ? t('dashboard.answerReviewSubmitting')
              : answer.source === 'off' ? t('dashboard.answerReviewClose') : t('dashboard.answerReviewSkip')}
          </button>
          {answer.source !== 'off' && (
            <button className="btn btn-primary" onClick={onConfirm} disabled={busy || !answer.answer_ready}>
            {busy ? t('dashboard.answerReviewSubmitting') : t('dashboard.answerReviewConfirm')}
            </button>
          )}
        </div>
      </section>
    </div>
  )
}

function formatEventLabel(event: ActivityEvent, t: (key: string) => string): string {
  const typeName = t(`events.${event.type}`) || event.type
  const lesson = event.lesson ? `[${event.lesson}] ` : ''

  switch (event.type) {
    case 'signin':
      return `${lesson}${typeName}: ${t(`events.${event.status || 'success'}`)}`
    case 'problem_received':
      return `${lesson}${typeName}`
    case 'problem': {
      const problemTypeName = event.problemtype
        ? t(`events.problemType${event.problemtype}`)
        : typeName
      if (event.status === 'ai_failed') {
        return `${lesson}${problemTypeName}: ${t('events.ai_failed')}`
      }
      const statusText = t(`events.${event.status || 'success'}`)
      const answerText = formatAnswer(event.answers)
      const sourceText = event.source ? ` [${t(`events.source_${event.source}`)}]` : ''
      return `${lesson}${problemTypeName}: ${statusText}${answerText ? `, ${t('events.answer')}: ${answerText}` : ''}${sourceText}`
    }
    case 'danmu':
      return `${lesson}${typeName}: "${event.content || ''}" — ${t(`events.${event.status || 'success'}`)}`
    case 'call':
      return `${lesson}${typeName}`
    case 'red_packet':
      return `${lesson}${typeName}: ${t(`events.${event.status || 'success'}`)}`
    case 'session_expired':
      return `${typeName}`
    case 'lesson_end':
      return `${lesson}${typeName}`
    case 'lesson_start':
      return `${lesson}${typeName}`
    case 'network':
      return `${typeName}: ${event.message || ''}`
    default:
      return `${lesson}${typeName}${event.message ? ': ' + event.message : ''}`
  }
}

function buildSpeechText(event: ActivityEvent, isChinese: boolean): string {
  const lesson = event.lesson || ''
  switch (event.type) {
    case 'signin':
      return isChinese ? `${lesson}已签到` : `${lesson} checked in`
    case 'problem_received':
      return isChinese ? `${lesson}收到题目` : `${lesson} problem received`
    case 'problem':
      if (event.status === 'ai_failed') {
        return isChinese ? `${lesson}AI答题失败，请手动作答` : `${lesson} AI failed, please answer manually`
      }
      return isChinese ? `${lesson}已答题` : `${lesson} answered`
    case 'call':
      return isChinese ? '您被点名' : 'You were called on'
    case 'danmu':
      return isChinese ? '弹幕已发送' : 'Danmu sent'
    case 'red_packet':
      return isChinese ? `${lesson}已抢红包` : `${lesson} red packet grabbed`
    default:
      return ''
  }
}

function eventBadgeClass(event: ActivityEvent): string {
  if (event.type === 'session_expired') return 'badge badge-red'
  if (event.type === 'lesson_end') return 'badge badge-gray'
  if (event.type === 'lesson_start') return 'badge badge-green'
  if (event.type === 'red_packet') return event.status === 'success' ? 'badge badge-green' : 'badge badge-red'
  if (event.type === 'problem_received') return 'badge badge-blue'
  if (event.type === 'call') return 'badge badge-yellow'
  if (event.type === 'network')
    return event.status === 'error' ? 'badge badge-red' : 'badge badge-green'
  if (event.status === 'success') return 'badge badge-green'
  if (event.status === 'error' || event.status === 'ai_failed') return 'badge badge-red'
  return 'badge badge-blue'
}

export default function Dashboard() {
  const { t, i18n } = useTranslation()
  const { activeAccount } = useAccounts()
  const accountId = activeAccount?.id ?? null
  const [allCourses, setAllCourses] = useState<CourseItem[]>([])
  const [events, setEvents] = useState<ActivityEvent[]>([])
  const [qrUrl, setQrUrl] = useState('')
  const [qrState, setQrState] = useState<QRCheckinState | null>(null)
  const [qrSubmitting, setQrSubmitting] = useState(false)
  const [checkinSettings, setCheckinSettings] = useState<CheckinSourceSettings | null>(null)
  const [checkinSourceInput, setCheckinSourceInput] = useState('')
  const [checkinSourceSaveStatus, setCheckinSourceSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [autoCheckinMode, setAutoCheckinMode] = useState<AutoCheckinMode | null>(null)
  const [autoCheckinTime, setAutoCheckinTime] = useState('')
  const [savedAutoCheckinMode, setSavedAutoCheckinMode] = useState<AutoCheckinMode | null>(null)
  const [savedAutoCheckinTime, setSavedAutoCheckinTime] = useState('')
  const [autoCheckinSaveStatus, setAutoCheckinSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [aiAnsweringMode, setAiAnsweringMode] = useState<AiAnsweringMode | null>(null)
  const [aiApiKeyConfigured, setAiApiKeyConfigured] = useState<boolean | null>(null)
  const [aiAnsweringSaveStatus, setAiAnsweringSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [clearingEvents, setClearingEvents] = useState(false)
  const [pendingAnswers, setPendingAnswers] = useState<PendingAnswer[]>([])
  const [answerActionId, setAnswerActionId] = useState<string | null>(null)
  const [answerActionError, setAnswerActionError] = useState('')
  const logRef = useRef<HTMLDivElement>(null)

  const voiceConfigsRef = useRef<Record<string, VoiceConfig>>({})
  const notifConfigsRef = useRef<Record<string, VoiceConfig>>({})
  const lessonToClassroomRef = useRef<Record<string, string>>({})
  const langRef = useRef(i18n.language)

  useEffect(() => {
    langRef.current = i18n.language
  }, [i18n.language])

  const fetchAllCourses = useCallback(() => {
    if (!accountId) return
    fetch(`/api/accounts/${accountId}/courses/all`)
      .then((r) => r.json())
      .then((data: CourseItem[]) => setAllCourses(data))
      .catch(() => {})
  }, [accountId])

  const fetchLessons = useCallback(() => {
    if (!accountId) return
    fetch(`/api/accounts/${accountId}/courses/active`)
      .then((r) => r.json())
      .then((data: { lessons: ActiveLesson[] }) => {
        const map: Record<string, string> = {}
        for (const l of data.lessons) {
          map[String(l.lessonid)] = String(l.classroomid)
        }
        lessonToClassroomRef.current = map
      })
      .catch(() => {})
  }, [accountId])

  const fetchCourseConfigs = useCallback(() => {
    if (!accountId) return
    fetch(`/api/accounts/${accountId}/courses/settings`)
      .then((r) => r.json())
      .then((data: Record<string, { notification?: VoiceConfig; voice_notification?: VoiceConfig }>) => {
        const voiceMap: Record<string, VoiceConfig> = {}
        const notifMap: Record<string, VoiceConfig> = {}
        const defaults: VoiceConfig = { enabled: true, signin: true, problem: true, call: true, danmu: false, red_packet: true }
        for (const [id, cfg] of Object.entries(data)) {
          notifMap[id] = cfg.notification ?? { ...defaults }
          voiceMap[id] = cfg.voice_notification ?? { ...defaults, enabled: false }
        }
        notifConfigsRef.current = notifMap
        voiceConfigsRef.current = voiceMap
      })
      .catch(() => {})
  }, [accountId])

  const fetchMonitorSettings = useCallback(() => {
    if (!accountId) return
    Promise.all([
      fetch(`/api/accounts/${accountId}/checkin-source`).then((r) => r.json()),
      fetch(`/api/accounts/${accountId}/auto-checkin`).then((r) => r.json()),
      fetch(`/api/accounts/${accountId}/ai-answering`).then((r) => r.json()),
    ])
      .then(([source, auto, ai]: [CheckinSourceSettings, AutoCheckinSettings, AiAnsweringSettings]) => {
        setCheckinSettings(source)
        setCheckinSourceInput(String(source.checkin_source))
        const mode = normalizeAutoCheckinMode(auto)
        const scheduleTime = auto.auto_checkin_time || auto.default_time || '08:00'
        setAutoCheckinMode(mode)
        setAutoCheckinTime(scheduleTime)
        setSavedAutoCheckinMode(mode)
        setSavedAutoCheckinTime(scheduleTime)
        setAiAnsweringMode(normalizeAiAnsweringMode(ai))
        setAiApiKeyConfigured(ai.ai_api_key_configured)
      })
      .catch(() => {
        setCheckinSettings(null)
        setAutoCheckinMode(null)
        setAutoCheckinTime('')
        setSavedAutoCheckinMode(null)
        setSavedAutoCheckinTime('')
        setAiAnsweringMode(null)
        setAiApiKeyConfigured(null)
      })
  }, [accountId])

  const fetchPendingAnswers = useCallback(() => {
    if (!accountId) return
    fetch(`/api/accounts/${accountId}/pending-answers`)
      .then((response) => {
        if (!response.ok) throw new Error('Failed to load pending answers')
        return response.json() as Promise<{ answers?: Record<string, unknown>[] }>
      })
      .then((data) => {
        setPendingAnswers((data.answers ?? []).map(normalizePendingAnswer).filter((item): item is PendingAnswer => item !== null))
      })
      .catch(() => {})
  }, [accountId])

  // Reload whenever active account changes
  useEffect(() => {
    if (!accountId) {
      setAllCourses([])
      setEvents([])
      setQrUrl('')
      setQrState(null)
      setCheckinSettings(null)
      setCheckinSourceInput('')
      setCheckinSourceSaveStatus('idle')
      setAutoCheckinMode(null)
      setAutoCheckinTime('')
      setSavedAutoCheckinMode(null)
      setSavedAutoCheckinTime('')
      setAutoCheckinSaveStatus('idle')
      setAiAnsweringMode(null)
      setAiApiKeyConfigured(null)
      setAiAnsweringSaveStatus('idle')
      setPendingAnswers([])
      setAnswerActionId(null)
      setAnswerActionError('')
      return
    }
    setEvents([]) // clear stale events from previous account
    setPendingAnswers([])
    setAnswerActionId(null)
    setAnswerActionError('')
    fetchAllCourses()
    fetchLessons()
    fetchCourseConfigs()
    fetchMonitorSettings()
    fetchPendingAnswers()
  }, [accountId, fetchAllCourses, fetchLessons, fetchCourseConfigs, fetchMonitorSettings, fetchPendingAnswers])

  const handleSaveCheckinSource = async () => {
    if (!accountId || !checkinSettings) return
    const source = Number(checkinSourceInput)
    if (!Number.isInteger(source) || !checkinSettings.options.some((option) => option.value === source)) {
      setCheckinSourceSaveStatus('error')
      return
    }

    setCheckinSourceSaveStatus('saving')
    try {
      const response = await fetch(`/api/accounts/${accountId}/checkin-source`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ checkin_source: source }),
      })
      if (!response.ok) throw new Error('Save failed')
      const data = await response.json() as { checkin_source: number }
      setCheckinSettings((previous) => previous ? { ...previous, checkin_source: data.checkin_source } : previous)
      setCheckinSourceInput(String(data.checkin_source))
      setCheckinSourceSaveStatus('saved')
      setTimeout(() => setCheckinSourceSaveStatus('idle'), 2000)
    } catch {
      setCheckinSourceSaveStatus('error')
    }
  }

  const handleSaveAutoCheckin = async () => {
    if (!accountId || autoCheckinMode === null || savedAutoCheckinMode === null) return
    const previousMode = savedAutoCheckinMode
    const previousTime = savedAutoCheckinTime
    setAutoCheckinSaveStatus('saving')
    try {
      const response = await fetch(`/api/accounts/${accountId}/auto-checkin`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          auto_checkin_mode: autoCheckinMode,
          auto_checkin_time: autoCheckinTime,
        }),
      })
      if (!response.ok) throw new Error('Save failed')
      const data = await response.json() as AutoCheckinSettings
      const mode = normalizeAutoCheckinMode(data)
      const scheduleTime = data.auto_checkin_time || autoCheckinTime
      setAutoCheckinMode(mode)
      setAutoCheckinTime(scheduleTime)
      setSavedAutoCheckinMode(mode)
      setSavedAutoCheckinTime(scheduleTime)
      setAutoCheckinSaveStatus('saved')
      setTimeout(() => setAutoCheckinSaveStatus('idle'), 2000)
    } catch {
      setAutoCheckinMode(previousMode)
      setAutoCheckinTime(previousTime)
      setAutoCheckinSaveStatus('error')
    }
  }

  const handleAiAnsweringChange = async (mode: AiAnsweringMode) => {
    if (!accountId || aiAnsweringMode === null || mode === aiAnsweringMode) return
    const previous = aiAnsweringMode

    if (mode === 'ai' && !aiApiKeyConfigured) {
      window.alert(t('dashboard.aiApiKeyRequired'))
      return
    }

    setAiAnsweringMode(mode)
    setAiAnsweringSaveStatus('saving')
    try {
      const response = await fetch(`/api/accounts/${accountId}/ai-answering`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ai_answering_mode: mode }),
      })
      const data = await response.json() as AiAnsweringSettings & {
        detail?: { code?: string }
      }
      if (!response.ok) {
        if (data.detail?.code === 'ai_api_key_required') {
          window.alert(t('dashboard.aiApiKeyRequired'))
          setAiAnsweringMode(previous)
          setAiAnsweringSaveStatus('idle')
          return
        }
        throw new Error('Save failed')
      }
      setAiAnsweringMode(normalizeAiAnsweringMode(data))
      setAiAnsweringSaveStatus('saved')
      setTimeout(() => setAiAnsweringSaveStatus('idle'), 2000)
    } catch {
      setAiAnsweringMode(previous)
      setAiAnsweringSaveStatus('error')
    }
  }

  const handleQrCheckin = async () => {
    if (!accountId) return
    const value = qrUrl.trim()
    if (!value) {
      setQrState({ status: 'error', code: 'invalid_qr_url', message: t('dashboard.qrEmpty') })
      return
    }
    setQrSubmitting(true)
    setQrState({ status: 'scanning' })
    try {
      const response = await fetch(`/api/accounts/${accountId}/checkin/qr`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: value }),
      })
      const data = await response.json() as {
        ok?: boolean
        lesson_id?: string | number
        already_running?: boolean
        code?: string | number
        message?: string
      }
      if (!response.ok || !data.ok) {
        setQrState({
          status: 'error',
          code: data.code ?? response.status,
          message: data.message || t('dashboard.qrFailed'),
        })
        return
      }
      setQrState({
        status: 'success',
        lesson_id: data.lesson_id,
        already_running: data.already_running,
      })
      fetchAllCourses()
      fetchLessons()
    } catch {
      setQrState({ status: 'error', code: 'network_error', message: t('dashboard.qrFailed') })
    } finally {
      setQrSubmitting(false)
    }
  }

  const handleClearEvents = async () => {
    if (!accountId || clearingEvents || events.length === 0) return
    if (!window.confirm(t('dashboard.clearActivityConfirm'))) return

    setClearingEvents(true)
    try {
      const response = await fetch(`/api/accounts/${accountId}/events`, { method: 'DELETE' })
      if (!response.ok) throw new Error('Clear failed')
      setEvents([])
    } catch {
      window.alert(t('dashboard.clearActivityFailed'))
    } finally {
      setClearingEvents(false)
    }
  }

  const handleAnswerDecision = async (decision: 'confirm' | 'skip') => {
    const pending = pendingAnswers[0]
    if (!accountId || !pending || answerActionId) return

    setAnswerActionId(pending.answer_id)
    setAnswerActionError('')
    try {
      const response = await fetch(`/api/accounts/${accountId}/pending-answers/${pending.answer_id}/${decision}`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error('Answer decision failed')
      setPendingAnswers((previous) => previous.filter((item) => item.answer_id !== pending.answer_id))
    } catch {
      setAnswerActionError(t('dashboard.answerReviewFailed'))
    } finally {
      setAnswerActionId(null)
    }
  }

  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission()
    }
  }, [])

  function notify(event: ActivityEvent) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return
    const isChinese = langRef.current.startsWith('zh')
    const title = event.lesson ?? (isChinese ? '雨课堂助手' : 'Yuketang Helper')
    const body = buildSpeechText(event, isChinese)
    if (!body) return
    new Notification(title, { body, silent: true })
  }

  function speak(text: string) {
    if (!text || !window.speechSynthesis) return
    const utter = new SpeechSynthesisUtterance(text)
    utter.lang = langRef.current.startsWith('zh') ? 'zh-CN' : 'en-US'
    window.speechSynthesis.cancel()
    window.speechSynthesis.speak(utter)
  }

  // Per-account WebSocket subscription
  useEffect(() => {
    if (!accountId) return
    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let unmounted = false

    function connect() {
      if (unmounted || !accountId) return
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${protocol}://${window.location.host}/ws/accounts/${accountId}/events`)

      ws.onmessage = (ev) => {
        let msg: Record<string, unknown>
        try {
          msg = JSON.parse(ev.data as string) as Record<string, unknown>
        } catch {
          return
        }
        const t = msg['type'] as string
        if (t === 'heartbeat') return

        if (t === 'answer_pending') {
          const pending = normalizePendingAnswer(msg)
          if (pending) {
            setPendingAnswers((previous) => {
              const index = previous.findIndex((item) => item.answer_id === pending.answer_id)
              if (index < 0) return [...previous, pending]
              const next = [...previous]
              next[index] = pending
              return next
            })
          }
          return
        }

        if (t === 'answer_updated') {
          const answerId = msg['answer_id']
          if (typeof answerId === 'string') {
            setPendingAnswers((previous) => previous.map((item) => item.answer_id === answerId
              ? {
                ...item,
                answer: msg['answer'],
                source: msg['source'] === 'random' ? 'random' : msg['source'] === 'off' ? 'off' : item.source,
                answer_ready: msg['answer_ready'] === true,
              }
              : item))
          }
          return
        }

        if (t === 'answer_review_closed') {
          const answerId = msg['answer_id']
          if (typeof answerId === 'string') {
            setPendingAnswers((previous) => previous.filter((item) => item.answer_id !== answerId))
          }
          return
        }

        if (t === 'events_cleared') {
          setEvents([])
          return
        }

        if (t === 'history') {
          const raw = (msg['events'] as Record<string, unknown>[]) ?? []
          const historical: ActivityEvent[] = raw.map((m) => ({
            id: ++eventCounter,
            timestamp: (m['logged_at'] as string | undefined)?.slice(11, 19) ?? '',
            type: m['type'] as string,
            lesson: m['lesson'] as string | undefined,
            lessonid: m['lessonid'] as number | undefined,
            status: m['status'] as string | undefined,
            message: m['message'] as string | undefined,
            content: m['content'] as string | undefined,
            answers: m['answers'],
            problemid: m['problemid'],
            problemtype: m['problemtype'] as number | undefined,
            source: m['source'] as string | number | undefined,
            pending_answer_id: m['pending_answer_id'] as string | undefined,
          }))
          setEvents(historical.reverse())
          fetchAllCourses()
          fetchLessons()
          fetchCourseConfigs()
          fetchPendingAnswers()
          return
        }

        const event: ActivityEvent = {
          id: ++eventCounter,
          timestamp: new Date().toTimeString().slice(0, 8),
          type: msg['type'] as string,
          lesson: msg['lesson'] as string | undefined,
          lessonid: msg['lessonid'] as number | undefined,
          status: msg['status'] as string | undefined,
          message: msg['message'] as string | undefined,
          content: msg['content'] as string | undefined,
          answers: msg['answers'],
          problemid: msg['problemid'],
          problemtype: msg['problemtype'] as number | undefined,
          source: msg['source'] as string | number | undefined,
          pending_answer_id: msg['pending_answer_id'] as string | undefined,
        }

        if (event.pending_answer_id) {
          setPendingAnswers((previous) => previous.filter((item) => item.answer_id !== event.pending_answer_id))
        }
        setEvents((prev) => [event, ...prev].slice(0, 50))

        if (event.type === 'lesson_start' || event.type === 'lesson_end') {
          fetchAllCourses()
          fetchLessons()
          fetchCourseConfigs()
        }

        const subKey = VOICE_SUBOPTION[event.type]
        if (subKey) {
          const courseId = lessonToClassroomRef.current[String(event.lessonid)] ?? String(event.lessonid)
          const notifCfg = notifConfigsRef.current[courseId]
          if (notifCfg?.enabled && notifCfg[subKey]) {
            notify(event)
          }
          const voiceCfg = voiceConfigsRef.current[courseId]
          if (voiceCfg?.enabled && voiceCfg[subKey]) {
            speak(buildSpeechText(event, langRef.current.startsWith('zh')))
          }
        }
      }

      ws.onerror = () => {}
      ws.onclose = () => {
        if (!unmounted) {
          reconnectTimer = setTimeout(connect, 3000)
        }
      }
    }

    connect()

    return () => {
      unmounted = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [accountId, fetchAllCourses, fetchLessons, fetchCourseConfigs, fetchPendingAnswers])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = 0
    }
  }, [events])

  return (
    <div className="page">
      {pendingAnswers.length > 0 && (
        <AnswerReviewModal
          answer={pendingAnswers[0]}
          busy={answerActionId === pendingAnswers[0].answer_id}
          error={answerActionError}
          onConfirm={() => void handleAnswerDecision('confirm')}
          onSkip={() => void handleAnswerDecision('skip')}
        />
      )}
      <section className="card qr-checkin-card">
        <div className="qr-checkin-heading">
          <div>
            <h2 className="card-title">{t('dashboard.qrCheckin')}</h2>
            <p className="card-description">{t('dashboard.qrCheckinDesc')}</p>
          </div>
          <span className="badge badge-blue">{t('dashboard.qrSourceBadge')}</span>
        </div>
        <div className="qr-checkin-form">
          <input
            className="form-input"
            type="text"
            value={qrUrl}
            placeholder={t('dashboard.qrPlaceholder')}
            onChange={(e) => {
              setQrUrl(e.target.value)
              if (qrState?.status === 'error') setQrState(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void handleQrCheckin()
            }}
            disabled={qrSubmitting}
          />
          <button className="btn btn-primary" onClick={() => void handleQrCheckin()} disabled={qrSubmitting}>
            {qrSubmitting ? t('dashboard.qrChecking') : t('dashboard.qrSubmit')}
          </button>
        </div>
        {qrState && (
          <div className={`qr-checkin-status ${qrState.status}`} aria-live="polite">
            {qrState.status === 'scanning' && (
              <>
                <div className="qr-progress-row active"><span className="qr-progress-dot" />{t('dashboard.qrScanning')}</div>
                <div className="qr-progress-row active"><span className="qr-progress-dot" />{t('dashboard.qrJoining')}</div>
              </>
            )}
            {qrState.status === 'success' && (
              <>
                <div className="qr-progress-row complete"><span className="qr-progress-dot" />{t('dashboard.qrParsed')}</div>
                <div className="qr-progress-row complete"><span className="qr-progress-dot" />{t('dashboard.qrLessonId')}: <code>{qrState.lesson_id}</code></div>
                <div className="qr-progress-row complete"><span className="qr-progress-dot" />{qrState.already_running ? t('dashboard.qrAlreadyRunning') : t('dashboard.qrSuccess')}</div>
              </>
            )}
            {qrState.status === 'error' && (
              <div className="qr-error-message">
                <strong>{t('dashboard.qrFailed')}</strong>
                <span>{qrState.message}</span>
                {qrState.code !== undefined && <code>{String(qrState.code)}</code>}
              </div>
            )}
          </div>
        )}
      </section>
      <section className="card dashboard-monitor-card">
        <div className="dashboard-settings-heading">
          <div>
            <h2 className="card-title">{t('dashboard.monitorSettings')}</h2>
            <p className="card-description">{t('dashboard.monitorSettingsDesc')}</p>
          </div>
        </div>
        <div className="dashboard-settings-grid">
          <div className="dashboard-setting">
            <div className="dashboard-setting-copy">
              <span className="form-label">{t('dashboard.checkinSource')}</span>
              <span className="dashboard-setting-help">{t('dashboard.checkinSourceDesc')}</span>
            </div>
            <div className="dashboard-setting-control">
              <div className="checkin-source-control">
                <select
                  className="form-select"
                  value={checkinSourceInput}
                  disabled={!checkinSettings || checkinSourceSaveStatus === 'saving'}
                  onChange={(e) => {
                    setCheckinSourceInput(e.target.value)
                    setCheckinSourceSaveStatus('idle')
                  }}
                >
                  {checkinSettings?.options.map((option) => (
                    <option key={option.value} value={option.value}>
                      {i18n.language.startsWith('zh') ? option.label_zh : option.label} ({option.value})
                    </option>
                  ))}
                </select>
                <button
                  className={`btn btn-sm ${checkinSourceSaveStatus === 'saved' ? 'btn-success' : checkinSourceSaveStatus === 'error' ? 'btn-danger' : 'btn-primary'}`}
                  onClick={() => void handleSaveCheckinSource()}
                  disabled={!checkinSettings || checkinSourceSaveStatus === 'saving' || checkinSourceInput === String(checkinSettings?.checkin_source ?? '')}
                >
                  {checkinSourceSaveStatus === 'saving'
                    ? t('settings.applying')
                    : checkinSourceSaveStatus === 'saved'
                      ? t('settings.applied')
                      : t('settings.apply')}
                </button>
              </div>
              {checkinSourceSaveStatus === 'error' && <span className="dashboard-setting-status error">{t('dashboard.saveFailed')}</span>}
            </div>
          </div>

          <div className="dashboard-setting">
            <div className="dashboard-setting-copy">
              <span className="form-label">{t('dashboard.autoCheckin')}</span>
              <span className="dashboard-setting-help">{t('dashboard.autoCheckinDesc')}</span>
            </div>
            <div className="dashboard-setting-control">
              <div className="checkin-source-control">
                <select
                  className="form-select"
                  aria-label={t('dashboard.autoCheckin')}
                  value={autoCheckinMode ?? 'off'}
                  onChange={(event) => {
                    setAutoCheckinMode(event.target.value as AutoCheckinMode)
                    setAutoCheckinSaveStatus('idle')
                  }}
                  disabled={autoCheckinMode === null || autoCheckinSaveStatus === 'saving'}
                >
                  <option value="on">{t('dashboard.autoCheckinOn')}</option>
                  <option value="scheduled">{t('dashboard.autoCheckinScheduled')}</option>
                  <option value="off">{t('dashboard.autoCheckinOff')}</option>
                </select>
                {autoCheckinMode === 'scheduled' && (
                  <input
                    type="time"
                    className="form-input-time"
                    aria-label={t('dashboard.autoCheckinTime')}
                    value={autoCheckinTime}
                    onChange={(event) => {
                      setAutoCheckinTime(event.target.value)
                      setAutoCheckinSaveStatus('idle')
                    }}
                    disabled={autoCheckinSaveStatus === 'saving'}
                  />
                )}
                <button
                  className={`btn btn-sm ${autoCheckinSaveStatus === 'saved' ? 'btn-success' : autoCheckinSaveStatus === 'error' ? 'btn-danger' : 'btn-primary'}`}
                  onClick={() => void handleSaveAutoCheckin()}
                  disabled={
                    autoCheckinMode === null
                    || savedAutoCheckinMode === null
                    || autoCheckinSaveStatus === 'saving'
                    || (
                      autoCheckinMode === savedAutoCheckinMode
                      && autoCheckinTime === savedAutoCheckinTime
                    )
                  }
                >
                  {autoCheckinSaveStatus === 'saving'
                    ? t('settings.applying')
                    : autoCheckinSaveStatus === 'saved'
                      ? t('settings.applied')
                      : t('settings.apply')}
                </button>
              </div>
              {autoCheckinSaveStatus === 'saving' && <span className="dashboard-setting-status">{t('settings.applying')}</span>}
              {autoCheckinSaveStatus === 'saved' && <span className="dashboard-setting-status success">{t('settings.applied')}</span>}
              {autoCheckinSaveStatus === 'error' && <span className="dashboard-setting-status error">{t('dashboard.saveFailed')}</span>}
            </div>
          </div>

          <div className="dashboard-setting">
            <div className="dashboard-setting-copy">
              <span className="form-label">{t('dashboard.aiAnswering')}</span>
              <span className="dashboard-setting-help">{t('dashboard.aiAnsweringDesc')}</span>
            </div>
            <div className="dashboard-setting-control">
              <select
                className="form-select"
                aria-label={t('dashboard.aiAnswering')}
                value={aiAnsweringMode ?? 'off'}
                onChange={(event) => void handleAiAnsweringChange(event.target.value as AiAnsweringMode)}
                disabled={aiAnsweringMode === null || aiAnsweringSaveStatus === 'saving'}
              >
                <option value="ai">AI</option>
                <option value="random">{t('settings.random')}</option>
                <option value="off">{t('settings.disabled')}</option>
              </select>
              {aiAnsweringSaveStatus === 'saving' && <span className="dashboard-setting-status">{t('settings.applying')}</span>}
              {aiAnsweringSaveStatus === 'saved' && <span className="dashboard-setting-status success">{t('settings.applied')}</span>}
              {aiAnsweringSaveStatus === 'error' && <span className="dashboard-setting-status error">{t('dashboard.saveFailed')}</span>}
            </div>
          </div>
        </div>
      </section>
      <section className="card">
        <h2 className="card-title">{t('dashboard.allCourses')}</h2>
        {allCourses.length === 0 ? (
          <p className="empty-message">{t('dashboard.noCourses')}</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('dashboard.course')}</th>
                <th>{t('dashboard.teacher')}</th>
                <th>{t('dashboard.status')}</th>
              </tr>
            </thead>
            <tbody>
              {allCourses.map((course) => (
                <tr key={course.classroom_id}>
                  <td>{course.name}</td>
                  <td>{course.teacher_name ?? t('common.unknown')}</td>
                  <td>
                    <span className={`badge ${course.active ? 'badge-green' : 'badge-gray'}`}>
                      {course.active ? t('dashboard.active') : t('dashboard.inactive')}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <div className="activity-heading">
          <h2 className="card-title">{t('dashboard.recentActivity')}</h2>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => void handleClearEvents()}
            disabled={clearingEvents || events.length === 0}
          >
            {clearingEvents ? t('dashboard.clearingActivity') : t('dashboard.clearActivity')}
          </button>
        </div>
        {events.length === 0 ? (
          <p className="empty-message">{t('dashboard.noActivity')}</p>
        ) : (
          <div className="activity-log" ref={logRef}>
            {events.map((event) => (
              <div key={event.id} className="activity-entry">
                <span className="activity-time">{event.timestamp}</span>
                <span className={eventBadgeClass(event)}>
                  {event.type === 'problem' && event.problemtype
                    ? t(`events.problemType${event.problemtype}`)
                    : t(`events.${event.type}`) || event.type}
                </span>
                <span className="activity-text">{formatEventLabel(event, t)}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
