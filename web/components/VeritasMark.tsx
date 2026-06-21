// The Veritas brand mark — a gold filament lightbulb over the VERITAS wordmark.
// Recreated as crisp SVG so it can be used as the full-page backdrop at any size.
// To use the exact PNG instead, drop it at public/veritas-logo.png and swap this
// for an <img src="/veritas-logo.png" />.

export default function VeritasMark({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 240 330"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden
    >
      <defs>
        <linearGradient id="veritas-gold" x1="40" y1="20" x2="205" y2="320" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#f8ecb8" />
          <stop offset="0.42" stopColor="#cda851" />
          <stop offset="0.72" stopColor="#8c6b32" />
          <stop offset="1" stopColor="#f1dc94" />
        </linearGradient>
      </defs>

      {/* bulb glass */}
      <path
        d="M120 34 C 77 34 48 66 48 106 C 48 134 63 152 76 167 C 85 177 89 183 89 193 L 151 193 C 151 183 155 177 164 167 C 177 152 192 134 192 106 C 192 66 163 34 120 34 Z"
        stroke="url(#veritas-gold)"
        strokeWidth="3.6"
        strokeLinejoin="round"
      />
      {/* filament — a slim crossing V */}
      <path
        d="M106 95 C 118 132 118 152 120 188 M134 95 C 122 132 122 152 120 188"
        stroke="url(#veritas-gold)"
        strokeWidth="3.2"
        strokeLinecap="round"
      />
      {/* screw base */}
      <path
        d="M93 201 H147 M91 213 H149 M93 225 H147"
        stroke="url(#veritas-gold)"
        strokeWidth="4"
        strokeLinecap="round"
      />
      <path
        d="M104 235 H136 L129 250 H111 Z"
        stroke="url(#veritas-gold)"
        strokeWidth="3.2"
        strokeLinejoin="round"
      />

      {/* wordmark */}
      <text
        x="120"
        y="312"
        textAnchor="middle"
        fontFamily="Georgia, 'Times New Roman', serif"
        fontSize="44"
        fontWeight="600"
        letterSpacing="5"
        fill="url(#veritas-gold)"
      >
        VERITAS
      </text>
    </svg>
  );
}
