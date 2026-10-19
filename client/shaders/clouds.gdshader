shader_type spatial;
// Fog of war: dark, soft, slowly drifting clouds over unexplored tiles and outside the map.
render_mode blend_mix, depth_draw_never, cull_disabled, unshaded, shadows_disabled, fog_disabled;

uniform sampler2D noise_tex : filter_linear_mipmap, repeat_enable;
uniform sampler2D fog_tex : filter_linear, repeat_disable;
uniform float map_size = 48.0;
uniform float border = 10.0;
uniform bool underground = false;

varying vec3 wpos;

// explored value with a soft fade over a few cells beyond the map edge
float fog_at(sampler2D ft, vec2 p, float msize, float brd) {
	vec2 pc = clamp(p, vec2(0.5), vec2(msize - 0.5));
	float f = texture(ft, (pc + brd) / (msize + 2.0 * brd)).r;
	vec2 o = max(max(-p, p - msize), vec2(0.0));
	float outside = length(o);
	return f * (1.0 - smoothstep(0.0, 3.5, outside));
}

void vertex() {
	wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
}

void fragment() {
	vec2 p = wpos.xz;
	float t = TIME;
	float fogv = fog_at(fog_tex, p, map_size, border);
	float n1 = texture(noise_tex, p * 0.022 + vec2(t * 0.004, t * 0.003)).r;
	float n2 = texture(noise_tex, p * 0.06 - vec2(t * 0.008, -t * 0.006)).r;
	float n3 = texture(noise_tex, p * 0.19 + vec2(t * 0.015, 0.0)).r;
	float dens = n1 * 0.6 + n2 * 0.3 + n3 * 0.1;
	// soft billowing edge towards the explored area
	float cover = 1.0 - smoothstep(0.05, 0.65, fogv + (n2 - 0.5) * 0.55 + (n3 - 0.5) * 0.2);
	float a = cover * mix(0.86, 0.98, smoothstep(0.35, 0.65, dens));
	vec3 base = underground ? vec3(0.012, 0.012, 0.016) : vec3(0.035, 0.04, 0.052);
	vec3 hi = underground ? vec3(0.05, 0.045, 0.05) : vec3(0.11, 0.115, 0.13);
	// brighter, sun-warmed rim where the cloud thins out over explored land
	float rim = smoothstep(0.35, 0.9, 1.0 - cover) * cover;
	vec3 col = mix(base, hi, smoothstep(0.45, 0.8, dens));
	col = mix(col, vec3(0.42, 0.38, 0.36), rim * 0.6);
	ALBEDO = col;
	ALPHA = clamp(a, 0.0, 0.98);
}
