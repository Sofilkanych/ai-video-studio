-- Експорт PPTX → PDF через Microsoft PowerPoint for Mac.
-- Виклик: osascript export_slides_powerpoint.applescript <src.pptx POSIX> <out.pdf POSIX>
-- Повертає "OK <кількість слайдів у PPTX, включно з прихованими>".
--
-- Перевірено у Фазі 0 (PowerPoint 16.113, macOS 27):
--  * PowerPoint відкриває файл з папки проєкту без діалогу доступу;
--  * копіювання в контейнер PowerPoint заборонене macOS для інших процесів — не використовуємо;
--  * save працює лише з об'єктом POSIX file; рядок POSIX або HFS мовчки нічого не пише;
--  * name презентації = ім'я файлу з розширенням;
--  * приховані слайди в PDF не потрапляють.
-- Ім'я файлу має бути унікальним (job_id), щоб не сплутати з відкритими презентаціями користувача.

on run argv
	set srcPath to item 1 of argv
	set pdfPath to item 2 of argv
	set srcFile to POSIX file srcPath
	set pdfFile to POSIX file pdfPath
	set fileName to do shell script "basename " & quoted form of srcPath

	tell application "Microsoft PowerPoint"
		try
			open srcFile
			-- open може повернути керування до завершення завантаження
			repeat 120 times
				if (exists presentation fileName) then exit repeat
				delay 0.5
			end repeat
			if not (exists presentation fileName) then error "presentation not found after open: " & fileName

			set thePres to presentation fileName
			set slideCount to count of slides of thePres
			save thePres in pdfFile as save as PDF
			close thePres saving no
		on error errMsg number errNum
			-- не залишати нашу презентацію відкритою в PowerPoint користувача
			try
				if (exists presentation fileName) then close presentation fileName saving no
			end try
			error errMsg number errNum
		end try
	end tell
	return "OK " & slideCount
end run
