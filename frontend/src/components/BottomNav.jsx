import { cx } from '../api.js'
import { Candle, Flask, Home, Layers, List } from './Icons.jsx'

export const TABS = [
  { id: 'home', label: 'Home', Icon: Home },
  { id: 'strategies', label: 'Strategies', Icon: Layers },
  { id: 'chart', label: 'Chart', Icon: Candle },
  { id: 'whatif', label: 'What-If', Icon: Flask },
  { id: 'activity', label: 'Activity', Icon: List },
]

/** Phone navigation: fixed bar at the bottom (hidden on desktop where everything is on one screen). */
export default function BottomNav({ active, onChange }) {
  return (
    <nav className="lg:hidden fixed bottom-0 inset-x-0 z-30 bg-panel/95 backdrop-blur border-t border-line safe-bottom">
      <ul className="grid grid-cols-5">
        {TABS.map(({ id, label, Icon }) => (
          <li key={id}>
            <button onClick={() => { onChange(id); window.scrollTo({ top: 0 }) }}
              className={cx('w-full flex flex-col items-center gap-0.5 py-2 text-[11px]', active === id ? 'text-accent' : 'text-muted')}>
              <Icon />{label}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}
