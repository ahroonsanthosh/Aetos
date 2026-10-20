/**
 * Weather as a sensing condition, not a widget.
 *
 * The point is not to display the weather. It is that fog costs the cameras and leaves the
 * radar alone, and the system should know the difference rather than reporting uniform
 * confidence in conditions that plainly do not support it.
 */

import type { Weather, WeatherCondition } from '@/types';

export const WEATHER: Record<WeatherCondition, Weather> = {
  clear: {
    condition: 'clear',
    label: 'Clear',
    sensorImpact: {},
  },
  rain: {
    condition: 'rain',
    label: 'Heavy rain',
    // Radar loses a little to attenuation; optics lose a lot; RF is indifferent.
    sensorImpact: { camera: 0.45, thermal: 0.7, radar: 0.9, acoustic: 0.5 },
  },
  fog: {
    condition: 'fog',
    label: 'Dense fog',
    // The instructive case: optics near-blind, radar and RF untouched.
    sensorImpact: { camera: 0.15, thermal: 0.55, radar: 1, rf: 1 },
  },
  wind: {
    condition: 'wind',
    label: 'Strong wind',
    sensorImpact: { acoustic: 0.3, camera: 0.85 },
  },
  night: {
    condition: 'night',
    label: 'Night',
    // Thermal gains relative to daylight cameras; this is the only case where an impact
    // is above 1, and the coverage model clamps it.
    sensorImpact: { camera: 0.3, thermal: 1 },
  },
};
