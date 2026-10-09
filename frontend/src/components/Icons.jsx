const base = { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }

export const Sun = (p) => (<svg {...base} {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>)
export const Moon = (p) => (<svg {...base} {...p}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>)
export const Shield = (p) => (<svg {...base} {...p}><path d="M12 3l8 3v6c0 4.5-3.2 8.2-8 9-4.8-.8-8-4.5-8-9V6l8-3z" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>)
export const Home = (p) => (<svg {...base} {...p}><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /></svg>)
export const Layers = (p) => (<svg {...base} {...p}><path d="M12 3l9 5-9 5-9-5 9-5z" /><path d="M3 13l9 5 9-5" /></svg>)
export const Candle = (p) => (<svg {...base} {...p}><path d="M7 3v3M7 14v4M17 6v3M17 17v3" /><rect x="5" y="6" width="4" height="8" rx="1" /><rect x="15" y="9" width="4" height="8" rx="1" /></svg>)
export const Flask = (p) => (<svg {...base} {...p}><path d="M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 0 0 5.8 21h12.4a1.5 1.5 0 0 0 1.3-2.5L14 9V3" /><path d="M7.5 15h9" /></svg>)
export const List = (p) => (<svg {...base} {...p}><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" /></svg>)
export const Power = (p) => (<svg {...base} {...p}><path d="M12 3v9" /><path d="M6.4 6.6a8 8 0 1 0 11.2 0" /></svg>)
