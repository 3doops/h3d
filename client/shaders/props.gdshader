shader_type spatial;
// Vertex-coloured props (buildings, rocks, piles) with triplanar stone/noise detail in world space.
render_mode blend_mix, depth_draw_opaque, cull_back, diffuse_burley, specular_schlick_ggx;

uniform sampler2D noise_tex : filter_linear_mipmap, repeat_enable;
uniform sampler2D crack_tex : filter_linear_mipmap, repeat_enable;
uniform float blocks = 1.0;

varying vec3 wpos;
varying vec3 wn;

void vertex() {
	wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
	wn = normalize((MODEL_MATRIX * vec4(NORMAL, 0.0)).xyz);
}

float tri(sampler2D t, vec3 p, vec3 n, float s) {
	vec3 w = pow(abs(n), vec3(4.0));
	w /= (w.x + w.y + w.z);
	return texture(t, p.zy * s).r * w.x + texture(t, p.xz * s).r * w.y + texture(t, p.xy * s).r * w.z;
}

void fragment() {
	float nz = tri(noise_tex, wpos, wn, 0.35);
	float fine = tri(noise_tex, wpos, wn, 2.5);
	float cr = tri(crack_tex, wpos, wn, 0.32);
	float mortar = smoothstep(0.02, 0.14, cr);
	float blockv = tri(noise_tex, floor(wpos * 6.0) / 6.0, wn, 3.1);
	vec3 c = COLOR.rgb * (0.78 + 0.32 * nz) * (0.9 + 0.2 * fine);
	c *= mix(1.0, mix(0.55, 0.9 + 0.25 * blockv, mortar), blocks);
	ALBEDO = c;
	ROUGHNESS = 0.86;
	SPECULAR = 0.3;
}
