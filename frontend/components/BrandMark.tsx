/**
 * 品牌标记：一个光圈（相机光圈叶片）。
 *
 * 它替代了上一版"自绘人像"式的图标——那个既不是作品也不能说明产品。光圈同时
 * 指向摄影与生成：光从中间进来，叶片决定画幅。纯几何，没有任何人脸描摹。
 */
export function BrandMark({ size = 30 }: { size?: number }) {
  const center = 20;
  const outer = 15;
  const inner = 6.4;
  const reach = 17;

  const blades = Array.from({ length: 6 }, (_, index) => {
    const angle = (Math.PI / 3) * index - Math.PI / 2;
    const cx = center + inner * Math.cos(angle);
    const cy = center + inner * Math.sin(angle);
    const dx = -Math.sin(angle) * reach;
    const dy = Math.cos(angle) * reach;
    return { key: index, x1: cx - dx, y1: cy - dy, x2: cx + dx, y2: cy + dy };
  });

  return (
    <svg
      className="brand__mark"
      width={size}
      height={size}
      viewBox="0 0 40 40"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <clipPath id="brand-aperture">
          <circle cx={center} cy={center} r={outer - 1.1} />
        </clipPath>
      </defs>
      <circle
        cx={center}
        cy={center}
        r={outer}
        stroke="currentColor"
        strokeOpacity="0.42"
        strokeWidth="1.1"
      />
      <g clipPath="url(#brand-aperture)" stroke="currentColor" strokeOpacity="0.92" strokeWidth="1.05">
        {blades.map((blade) => (
          <line key={blade.key} x1={blade.x1} y1={blade.y1} x2={blade.x2} y2={blade.y2} strokeLinecap="butt" />
        ))}
      </g>
      <circle cx={center} cy={center} r={outer} stroke="currentColor" strokeOpacity="0.16" strokeWidth="2.4" />
    </svg>
  );
}

/** 上传用的取景框标记：一个方框内嵌光圈，说明"照片会进来"。 */
export function FrameMark() {
  return (
    <svg className="drop__icon" viewBox="0 0 40 40" fill="none" aria-hidden="true" focusable="false">
      <path
        d="M6 13.5V8.5A2.5 2.5 0 0 1 8.5 6h5M26 6h5.5A2.5 2.5 0 0 1 34 8.5v5M34 26.5v5a2.5 2.5 0 0 1-2.5 2.5h-5M14 34H8.5A2.5 2.5 0 0 1 6 31.5v-5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <circle cx="20" cy="20" r="6.2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="20" cy="20" r="2" fill="currentColor" />
    </svg>
  );
}

/** 空状态标记：一支未曝光的光圈，语气克制。 */
export function EmptyMark() {
  return (
    <svg className="empty__mark" viewBox="0 0 40 40" fill="none" aria-hidden="true" focusable="false">
      <circle cx="20" cy="20" r="14" stroke="currentColor" strokeWidth="1.2" strokeDasharray="4 5" />
      <circle cx="20" cy="20" r="5" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

/** 等待中的光圈：叶片缓慢旋转，表达"正在曝光"，不表达百分比。 */
export function ApertureSpinner() {
  return (
    <svg className="aperture" viewBox="0 0 64 64" fill="none" aria-hidden="true" focusable="false">
      <circle cx="32" cy="32" r="24" stroke="currentColor" strokeOpacity="0.28" strokeWidth="1.4" />
      <g className="aperture__blades" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <path d="M32 8 32 32 12 44" />
        <path d="M54 20 32 32 32 56" />
        <path d="M44 54 32 32 8 32" />
      </g>
      <circle cx="32" cy="32" r="23" stroke="currentColor" strokeOpacity="0.12" strokeWidth="3" />
    </svg>
  );
}
