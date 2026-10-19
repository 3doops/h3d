shader_type spatial;
// Emissive vertex colour (windows, magic, fire).
render_mode blend_mix, depth_draw_opaque, cull_back, shadows_disabled;

uniform float energy = 2.5;
uniform float flicker = 0.15;

void fragment() {
	float f = 1.0 - flicker + flicker * sin(TIME * 7.0 + COLOR.r * 13.0) * sin(TIME * 3.1);
	ALBEDO = COLOR.rgb * 0.3;
	EMISSION = COLOR.rgb * energy * f;
	ROUGHNESS = 0.6;
}
