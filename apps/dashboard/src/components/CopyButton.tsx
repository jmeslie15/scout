import { useCallback, useEffect, useRef, useState } from 'react'

export interface CopyButtonProps {
  /** Text placed on the clipboard. */
  value: string
  /** Button label; defaults to "Copy". */
  label?: string
  /** Accessible description of what is copied, e.g. "commit SHA". */
  subject?: string
}

type CopyState = 'idle' | 'copied' | 'failed'

/**
 * Copy-to-clipboard button for SHAs, job ids, and artifact digests.
 *
 * Uses `navigator.clipboard` when available and falls back to a hidden
 * `<textarea>` + `document.execCommand('copy')` for non-secure origins, because
 * the dashboard is served over plain HTTP in dev and on internal hosts.
 * `aria-live` announces the result for screen-reader users.
 */
export function CopyButton({ value, label = 'Copy', subject }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>('idle')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current)
    }
  }, [])

  const announce = useCallback((next: CopyState) => {
    setState(next)
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setState('idle'), 2000)
  }, [])

  const copy = useCallback(async () => {
    const text = value
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
        announce('copied')
        return
      }
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(textarea)
      announce(ok ? 'copied' : 'failed')
    } catch {
      announce('failed')
    }
  }, [announce, value])

  const accessibleLabel = subject !== undefined ? `Copy ${subject}` : 'Copy to clipboard'

  return (
    <button
      type="button"
      className="button button--ghost button--small copy-button"
      onClick={() => {
        void copy()
      }}
      aria-label={accessibleLabel}
      title={accessibleLabel}
    >
      <span aria-hidden="true">{state === 'idle' ? label : state === 'copied' ? 'Copied' : 'Failed'}</span>
      <span className="visually-hidden" aria-live="polite">
        {state === 'copied'
          ? `${subject ?? 'Value'} copied to clipboard`
          : state === 'failed'
            ? `Could not copy ${subject ?? 'value'}`
            : ''}
      </span>
    </button>
  )
}
