-- calendar_export.scpt
-- 出力形式: start_epoch<TAB>end_epoch<TAB>title<TAB>location
--
-- NOTE:
-- - epoch は GMT を明示して Unix epoch と一致させる（タイムゾーン差でズレない）
-- - 日付文字列はロケール依存しにくい "YYYY-MM-DD HH:MM:SS GMT" を使用

on run argv
	set rangeDays to 30
	try
		if (count of argv) ≥ 1 then
			set rangeDays to (item 1 of argv) as integer
		end if
	end try
	if rangeDays < 1 then set rangeDays to 1
	if rangeDays > 365 then set rangeDays to 365

	set epoch to date "1970-01-01 00:00:00 GMT"

	set nowDate to (current date)
	set endDate to nowDate + (rangeDays * days)

	set outLines to {}
	do shell script "/usr/bin/open -gj -a Calendar"
	delay 0.5
	
	repeat with i from 1 to 20
		try
			tell application "Calendar"
				set calList to calendars
				
				repeat with cal in calList
					try
						set evList to (every event of cal whose start date ≥ nowDate and start date ≤ endDate)
						repeat with ev in evList
							set s to (start date of ev) - epoch
							set e to (end date of ev) - epoch
							set t to summary of ev
							
							set loc to ""
							try
								set loc to location of ev
							end try
							
							set end of outLines to ((s as integer) & tab & (e as integer) & tab & t & tab & loc)
						end repeat
					end try
				end repeat
			end tell
			exit repeat
		on error errMsg number errNum
			if errNum is -600 then
				delay 0.5
			else
				error errMsg number errNum
			end if
		end try
	end repeat

	repeat with lineText in outLines
		log lineText
	end repeat
end run
