-- ============================================================
-- MIGRASI SUPABASE: KOLOM SINKRONISASI DATABASE LOKAL (MINI PC)
-- Jalankan SEKALI di Supabase Dashboard -> SQL Editor -> Run.
-- Aman dijalankan ulang dan tidak menghapus data yang sudah ada.
-- ============================================================

-- 1. Waktu perubahan terakhir kendaraan: menentukan data mana yang lebih baru
--    saat kendaraan diubah di Mini PC dan di Supabase/Netlify pada waktu berdekatan.
ALTER TABLE public.registered_vehicles
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_registered_vehicles_updated_at ON public.registered_vehicles;
CREATE TRIGGER trg_registered_vehicles_updated_at
  BEFORE UPDATE ON public.registered_vehicles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 2. ID unik log dari Mini PC: mencegah log terkirim dobel saat pengiriman diulang.
ALTER TABLE public.vehicle_logs
  ADD COLUMN IF NOT EXISTS client_uuid UUID UNIQUE;

-- 3. Status perintah buka gerbang yang kedaluwarsa (dikirim saat Mini PC offline)
COMMENT ON COLUMN public.gate_commands.status IS 'PENDING, EXECUTED, EXPIRED';

-- 4. Kategori kendaraan: tambah OTHERS (selain STUDENT, LECTURER, ACADEMIC COMMUNITY)
ALTER TABLE public.registered_vehicles DROP CONSTRAINT IF EXISTS registered_vehicles_category_check;
ALTER TABLE public.registered_vehicles ADD CONSTRAINT registered_vehicles_category_check
  CHECK (category IN ('STUDENT', 'LECTURER', 'ACADEMIC COMMUNITY', 'OTHERS'));

-- 5. Status dibaca / dihapus Security Notification (tersinkron antar perangkat & Mini PC)
CREATE TABLE IF NOT EXISTS public.alert_states (
  log_uuid UUID PRIMARY KEY,
  is_read BOOLEAN NOT NULL DEFAULT FALSE,
  dismissed BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_alert_states_updated_at ON public.alert_states;
CREATE TRIGGER trg_alert_states_updated_at
  BEFORE UPDATE ON public.alert_states
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_alert_states_updated ON public.alert_states (updated_at);

ALTER TABLE public.alert_states ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow public read alert_states" ON public.alert_states;
DROP POLICY IF EXISTS "Allow public insert alert_states" ON public.alert_states;
DROP POLICY IF EXISTS "Allow public update alert_states" ON public.alert_states;
CREATE POLICY "Allow public read alert_states" ON public.alert_states FOR SELECT USING (true);
CREATE POLICY "Allow public insert alert_states" ON public.alert_states FOR INSERT WITH CHECK (true);
CREATE POLICY "Allow public update alert_states" ON public.alert_states FOR UPDATE USING (true);
