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

-- 5. Security Notification terdokumentasi di tabel security_alerts (menggantikan alert_states)
DROP TABLE IF EXISTS public.alert_states;

CREATE TABLE IF NOT EXISTS public.security_alerts (
  log_uuid UUID PRIMARY KEY,                   -- = vehicle_logs.client_uuid (log asal notifikasi)
  created_at TIMESTAMPTZ NOT NULL,             -- waktu kejadian (sama dengan log)
  alert_type VARCHAR(20) NOT NULL CHECK (alert_type IN ('UNREGISTERED', 'RFID_MISMATCH', 'MANUAL_OPEN', 'OCR_LOW')),
  severity VARCHAR(10) NOT NULL,               -- 'danger' / 'warning' / 'info'
  title VARCHAR(150) NOT NULL,
  description TEXT,
  plate VARCHAR(20),
  rfid_uid VARCHAR(50),                        -- kartu yang di-tap (kendaraan belum terdaftar)
  confidence NUMERIC(5, 2),
  is_read BOOLEAN NOT NULL DEFAULT FALSE,      -- sudah dibaca petugas
  dismissed BOOLEAN NOT NULL DEFAULT FALSE,    -- dihapus dari daftar notifikasi (log asli tetap ada)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_security_alerts_updated_at ON public.security_alerts;
CREATE TRIGGER trg_security_alerts_updated_at
  BEFORE UPDATE ON public.security_alerts
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_security_alerts_created ON public.security_alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_alerts_updated ON public.security_alerts (updated_at);

ALTER TABLE public.security_alerts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow public read security_alerts" ON public.security_alerts;
DROP POLICY IF EXISTS "Allow public insert security_alerts" ON public.security_alerts;
DROP POLICY IF EXISTS "Allow public update security_alerts" ON public.security_alerts;
CREATE POLICY "Allow public read security_alerts" ON public.security_alerts FOR SELECT USING (true);
CREATE POLICY "Allow public insert security_alerts" ON public.security_alerts FOR INSERT WITH CHECK (true);
CREATE POLICY "Allow public update security_alerts" ON public.security_alerts FOR UPDATE USING (true);
